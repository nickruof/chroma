import sys
import os
from hashlib import md5
import cPickle as pickle
import gzip
import numpy as np
import time

from chroma.itertoolset import *
from chroma.tools import timeit, profile_if_possible, filled_array, \
    memoize_method_with_dictionary_arg
from chroma.log import logger

# all material/surface properties are interpolated at these
# wavelengths when they are sent to the gpu
standard_wavelengths = np.arange(200, 810, 20).astype(np.float32)

class Mesh(object):
    "Triangle mesh object."
    def __init__(self, vertices, triangles, remove_duplicate_vertices=False):
        vertices = np.asarray(vertices, dtype=np.float32)
        triangles = np.asarray(triangles, dtype=np.int32)

        if len(vertices.shape) != 2 or vertices.shape[1] != 3:
            raise ValueError('shape mismatch')

        if len(triangles.shape) != 2 or triangles.shape[1] != 3:
            raise ValueError('shape mismatch')

        if (triangles < 0).any():
            raise ValueError('indices in `triangles` must be positive.')

        if (triangles >= len(vertices)).any():
            raise ValueError('indices in `triangles` must be less than the '
                             'length of the vertex array.')

        self.vertices = vertices
        self.triangles = triangles

        if remove_duplicate_vertices:
            self.remove_duplicate_vertices()

    def get_bounds(self):
        "Return the lower and upper bounds for the mesh as a tuple."
        return np.min(self.vertices, axis=0), np.max(self.vertices, axis=0)

    def remove_duplicate_vertices(self):
        "Remove any duplicate vertices in the mesh."
        # view the vertices as a structured array in order to identify unique
        # rows, i.e. unique vertices
        unique_vertices, inverse = np.unique(self.vertices.view([('', self.vertices.dtype)]*self.vertices.shape[1]), return_inverse=True)
        # turn the structured vertex array back into a normal array
        self.vertices = unique_vertices.view(self.vertices.dtype).reshape((unique_vertices.shape[0], self.vertices.shape[1]))
        # remap the triangles to the unique vertices
        self.triangles = inverse[self.triangles]

    def assemble(self, key=slice(None), group=True):
        """
        Return an assembled triangle mesh; i.e. return the vertex positions
        of every triangle. If `group` is True, the array returned will have
        an extra axis denoting the triangle number; i.e. if the mesh contains
        N triangles, the returned array will have the shape (N,3,3). If `group`
        is False, return just the vertex positions without any grouping; in
        this case the grouping is implied (i.e. elements [0:3] are the first
        triangle, [3:6] the second, and so on.

        The `key` argument is a slice object if you just want to assemble
        a certain range of the triangles, i.e. to get the assembled mesh for
        triangles 3 through 6, key = slice(3,7).
        """
        if group:
            vertex_indices = self.triangles[key]
        else:
            vertex_indices = self.triangles[key].flatten()

        return self.vertices[vertex_indices]

    def __add__(self, other):
        return Mesh(np.concatenate((self.vertices, other.vertices)), np.concatenate((self.triangles, other.triangles + len(self.vertices))))

    def md5(self):
        '''Return the MD5 hash of the vertices and triangles in this mesh as a 
        hexidecimal string.'''
        checksum = md5(self.vertices)
        checksum.update(self.triangles)
        return checksum.hexdigest()

class Solid(object):
    """Solid object attaches materials, surfaces, and colors to each triangle
    in a Mesh object."""
    def __init__(self, mesh, material1=None, material2=None, surface=None, color=0xffffff):
        self.mesh = mesh

        if np.iterable(material1):
            if len(material1) != len(mesh.triangles):
                raise ValueError('shape mismatch')
            self.material1 = np.array(material1, dtype=np.object)
        else:
            self.material1 = np.tile(material1, len(self.mesh.triangles))

        if np.iterable(material2):
            if len(material2) != len(mesh.triangles):
                raise ValueError('shape mismatch')
            self.material2 = np.array(material2, dtype=np.object)
        else:
            self.material2 = np.tile(material2, len(self.mesh.triangles))

        if np.iterable(surface):
            if len(surface) != len(mesh.triangles):
                raise ValueError('shape mismatch')
            self.surface = np.array(surface, dtype=np.object)
        else:
            self.surface = np.tile(surface, len(self.mesh.triangles))

        if np.iterable(color):
            if len(color) != len(mesh.triangles):
                raise ValueError('shape mismatch')
            self.color = np.array(color, dtype=np.uint32)
        else:
            self.color = np.tile(color, len(self.mesh.triangles)).astype(np.uint32)

        self.unique_materials = \
            np.unique(np.concatenate([self.material1, self.material2]))

        self.unique_surfaces = np.unique(self.surface)

    def __add__(self, other):
        return Solid(self.mesh + other.mesh, np.concatenate((self.material1, other.material1)), np.concatenate((self.material2, other.material2)), np.concatenate((self.surface, other.surface)), np.concatenate((self.color, other.color)))

    @memoize_method_with_dictionary_arg
    def material1_indices(self, material_lookup):
        return np.fromiter(imap(material_lookup.get, self.material1), dtype=np.int32, count=len(self.material1))

    @memoize_method_with_dictionary_arg
    def material2_indices(self, material_lookup):
        return np.fromiter(imap(material_lookup.get, self.material2), dtype=np.int32, count=len(self.material2))

    @memoize_method_with_dictionary_arg
    def surface_indices(self, surface_lookup):
        return np.fromiter(imap(surface_lookup.get, self.surface), dtype=np.int32, count=len(self.surface))

class Material(object):
    """Material optical properties."""
    def __init__(self, name='none'):
        self.name = name

        self.refractive_index = None
        self.absorption_length = None
        self.scattering_length = None
        self.density = 0.0 # g/cm^3
        self.composition = {} # by mass

    def set(self, name, value, wavelengths=standard_wavelengths):
        if np.iterable(value):
            if len(value) != len(wavelengths):
                raise ValueError('shape mismatch')
        else:
            value = np.tile(value, len(wavelengths))

        self.__dict__[name] = np.array(zip(wavelengths, value), dtype=np.float32)

# Empty material
vacuum = Material('vacuum')
vacuum.set('refractive_index', 1.0)
vacuum.set('absorption_length', 1e6)
vacuum.set('scattering_length', 1e6)


class Surface(object):
    """Surface optical properties."""
    def __init__(self, name='none', model=0):
        self.name = name
        self.model = model

        self.set('detect', 0)
        self.set('absorb', 0)
        self.set('reemit', 0)
        self.set('reflect_diffuse', 0)
        self.set('reflect_specular', 0)
        self.set('eta', 0)
        self.set('k', 0)
        self.set('reemission_cdf', 0)

        self.thickness = 0.0
        self.transmissive = 0

    def set(self, name, value, wavelengths=standard_wavelengths):
        if np.iterable(value):
            if len(value) != len(wavelengths):
                raise ValueError('shape mismatch')
        else:
            value = np.tile(value, len(wavelengths))

        if (np.asarray(value) < 0.0).any():
            raise Exception('all probabilities must be >= 0.0')

        self.__dict__[name] = np.array(zip(wavelengths, value), dtype=np.float32)
    def __repr__(self):
        return '<Surface %s>' % self.name
        
class Geometry(object):
    "Geometry object."
    def __init__(self, detector_material=None):
        self.detector_material = detector_material
        self.solids = []
        self.solid_rotations = []
        self.solid_displacements = []
        self.bvh = None

    def add_solid(self, solid, rotation=None, displacement=None):
        """
        Add the solid `solid` to the geometry. When building the final triangle
        mesh, `solid` will be placed by rotating it with the rotation matrix
        `rotation` and displacing it by the vector `displacement`.
        """

        if rotation is None:
            rotation = np.identity(3)
        else:
            rotation = np.asarray(rotation, dtype=np.float32)

        if rotation.shape != (3,3):
            raise ValueError('rotation matrix has the wrong shape.')

        self.solid_rotations.append(rotation.astype(np.float32))

        if displacement is None:
            displacement = np.zeros(3)
        else:
            displacement = np.asarray(displacement, dtype=np.float32)

        if displacement.shape != (3,):
            raise ValueError('displacement vector has the wrong shape.')

        self.solid_displacements.append(displacement)

        self.solids.append(solid)

        return len(self.solids)-1

    def flatten(self):
        """
        Create the flat list of triangles (and triangle properties)
        from the list of solids in this geometry.

        This does not build the BVH!  If you want to use the geometry
        for rendering or simulation, you should call build() instead.
        """

        # Don't run this function twice!
        if hasattr(self, 'mesh'):
            return

        nv = np.cumsum([0] + [len(solid.mesh.vertices) for solid in self.solids])
        nt = np.cumsum([0] + [len(solid.mesh.triangles) for solid in self.solids])

        vertices = np.empty((nv[-1],3), dtype=np.float32)
        triangles = np.empty((nt[-1],3), dtype=np.uint32)
        

        logger.info('Flattening detector mesh...')
        logger.info('  triangles: %d' % len(triangles))
        logger.info('  vertices:  %d' % len(vertices))


        for i, solid in enumerate(self.solids):
            vertices[nv[i]:nv[i+1]] = \
                np.inner(solid.mesh.vertices, self.solid_rotations[i]) + self.solid_displacements[i]
            triangles[nt[i]:nt[i+1]] = solid.mesh.triangles + nv[i]

        # Different solids are very unlikely to share vertices, so this goes much faster
        self.mesh = Mesh(vertices, triangles, remove_duplicate_vertices=False)

        self.colors = np.concatenate([solid.color for solid in self.solids])

        self.solid_id = np.concatenate([filled_array(i, shape=len(solid.mesh.triangles), dtype=np.uint32) for i, solid in enumerate(self.solids)])

        self.unique_materials = list(np.unique(np.concatenate([solid.unique_materials for solid in self.solids])))

        material_lookup = dict(zip(self.unique_materials, range(len(self.unique_materials))))

        self.material1_index = np.concatenate([solid.material1_indices(material_lookup) for solid in self.solids])

        self.material2_index = np.concatenate([solid.material2_indices(material_lookup) for solid in self.solids])

        self.unique_surfaces = list(np.unique(np.concatenate([solid.unique_surfaces for solid in self.solids])))

        surface_lookup = dict(zip(self.unique_surfaces, range(len(self.unique_surfaces))))

        self.surface_index = np.concatenate([solid.surface_indices(surface_lookup) for solid in self.solids])

        try:
            self.surface_index[self.surface_index == surface_lookup[None]] = -1
        except KeyError:
            pass
