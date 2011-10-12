// -*-c++-*-
#include "detector.h"
#include "random.h"

__device__ unsigned int
float_to_sortable_int(float f)
{
    return __float_as_int(f);
    //int i = __float_as_int(f);
    //unsigned int mask = -(int)(i >> 31) | 0x80000000;
    //return i ^ mask;
}

__device__ float
sortable_int_to_float(unsigned int i)
{
    return __int_as_float(i);
    //unsigned int mask = ((i >> 31) - 1) | 0x80000000;
    //return __int_as_float(i ^ mask);
}

extern "C"
{

__global__ void
reset_earliest_time_int(float maxtime, int ntime_ints, unsigned int *time_ints)
{
    int id = threadIdx.x + blockDim.x * blockIdx.x;
    if (id < ntime_ints) {
	unsigned int maxtime_int = float_to_sortable_int(maxtime);
	time_ints[id] = maxtime_int;
    }
}

__global__ void
run_daq(curandState *s, unsigned int detection_state,
	int first_photon, int nphotons, float *photon_times,
	unsigned int *photon_histories, int *last_hit_triangles,
	int *solid_map,
	Detector *detector,
	unsigned int *earliest_time_int,
	unsigned int *channel_q_int, unsigned int *channel_histories)
{

    int id = threadIdx.x + blockDim.x * blockIdx.x;

    if (id < nphotons) {
	curandState rng = s[id];
	int photon_id = id + first_photon;
	int triangle_id = last_hit_triangles[photon_id];
		
	if (triangle_id > -1) {
	    int solid_id = solid_map[triangle_id];
	    unsigned int history = photon_histories[photon_id];
	    int channel_index = detector->solid_id_to_channel_index[solid_id];

	    if (channel_index >= 0 && (history & detection_state)) {
		float time = photon_times[photon_id] + 
		    sample_cdf(&rng, detector->time_cdf_len,
			       detector->time_cdf_x, detector->time_cdf_y);
		unsigned int time_int = float_to_sortable_int(time);
			  
		float charge = sample_cdf(&rng, detector->charge_cdf_len,
					  detector->charge_cdf_x,
					  detector->charge_cdf_y);
		unsigned int charge_int = roundf(charge / detector->charge_unit);

		atomicMin(earliest_time_int + channel_index, time_int);
		atomicAdd(channel_q_int + channel_index, charge_int);
		atomicOr(channel_histories + channel_index, history);
	    }

	}

	s[id] = rng;
	
    }
    
}

__global__ void
convert_sortable_int_to_float(int n, unsigned int *sortable_ints,
			      float *float_output)
{
    int id = threadIdx.x + blockDim.x * blockIdx.x;
	
    if (id < n)
	float_output[id] = sortable_int_to_float(sortable_ints[id]);
}


__global__ void
convert_charge_int_to_float(Detector *detector, 
			    unsigned int *charge_int,
			    float *charge_float)
{
    int id = threadIdx.x + blockDim.x * blockIdx.x;
	
    if (id < detector->nchannels)
	charge_float[id] = charge_int[id] * detector->charge_unit;
}


} // extern "C"