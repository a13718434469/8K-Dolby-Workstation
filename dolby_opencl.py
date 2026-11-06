"""Optional FP64 GPU implementation of the full-frame Profile 8.4 measurements."""
import numpy as np


_SOURCE = r"""
#pragma OPENCL EXTENSION cl_khr_fp64 : enable
#pragma OPENCL FP_CONTRACT OFF
inline double poly7(double a, double b, double c, __constant double *k) {
    double terms[7] = {a,b,c,a*b,a*c,b*c,a*b*c};
    double z = k[0];
    for (int i=0;i<3;i++) for (int j=0;j<7;j++) {
        double t=terms[j]; z += k[1+i*7+j]*(i==0?t:i==1?t*t:t*t*t);
    }
    return clamp(z,0.0,1.0);
}
__kernel void compose(__global const ushort *y, __global const ushort *u, __global const ushort *v,
                      __global double2 *uv, __constant double *mmr, __constant double *bounds, int w, int h) {
    int p=get_global_id(0); if(p>=w*h/4) return;
    int x=(p%(w/2))*2, row=(p/(w/2))*2*w, l=max(x-1,0), r=min(x+1,w-1);
    int top=(y[row+l]+2*y[row+x]+y[row+r]+2)>>2;
    row+=w;
    int bot=(y[row+l]+2*y[row+x]+y[row+r]+2)>>2;
    double a=clamp(((top+bot+1)>>1)/1024.0,bounds[0],bounds[1]);
    double b=clamp(u[p]/1024.0,bounds[2],bounds[3]), c=clamp(v[p]/1024.0,bounds[4],bounds[5]);
    uv[p]=(double2)(poly7(a,b,c,mmr),poly7(a,b,c,mmr+22));
}
inline double3 mul3(double3 v, __constant double *m) {
    return (double3)(v.x*m[0]+v.y*m[1]+v.z*m[2],v.x*m[3]+v.y*m[4]+v.z*m[5],v.x*m[6]+v.y*m[7]+v.z*m[8]);
}
inline double3 rgb(__global const ushort *y, __global const double2 *uv, __constant double *lut,
                   __constant double *mat, int x, int row, int w, int h, double shift) {
    int x0=x/2, x1=min(x0+1,w/2-1);
    double fy=row*0.5-shift, wy=fy-floor(fy), wx=(x%2)*0.5;
    int y0=clamp((int)floor(fy),0,h/2-1)*(w/2), y1=clamp((int)floor(fy)+1,0,h/2-1)*(w/2);
    double2 chroma=(uv[y0+x0]*(1.0-wx)+uv[y0+x1]*wx)*(1.0-wy)+(uv[y1+x0]*(1.0-wx)+uv[y1+x1]*wx)*wy;
    double3 a=(double3)(lut[y[row*w+x]],chroma.x,chroma.y)-vload3(0,mat+9);
    double3 v=pow(clamp(mul3(a,mat),0.0,1.0),(double3)(32.0/2523.0));
    double3 linear=pow(fmax(v-3424.0/4096.0,0.0)/fmax(2413.0/128.0-2392.0/128.0*v,1e-15),(double3)(16384.0/2610.0));
    v=pow(fmax(mul3(linear,mat+12),0.0),(double3)(2610.0/16384.0));
    return clamp(pow((3424.0/4096.0+2413.0/128.0*v)/(1.0+2392.0/128.0*v),(double3)(2523.0/32.0)),0.0,1.0);
}
__kernel void measure(__global const ushort *y, __global const double2 *uv, __constant double *lut,
                      __constant double *mat, int w, int h, double shift, __global double *out) {
    int p=get_global_id(0), t=get_local_id(0);
    __local double lo[128], hi[128], sum[128];
    lo[t]=1.0; hi[t]=0.0; sum[t]=0.0;
    if(p<w*h/4){
        int x=2*(p%(w/2)), row=2*(p/(w/2));
        double3 v=(double3)(0.0);
        for(int j=0;j<2;j++) for(int i=0;i<2;i++) v+=rgb(y,uv,lut,mat,x+i,row+j,w,h,shift);
        v *= 0.25; lo[t]=fmin(v.x,fmin(v.y,v.z)); hi[t]=sum[t]=fmax(v.x,fmax(v.y,v.z));
    }
    barrier(CLK_LOCAL_MEM_FENCE);
    for(int n=64;n>0;n/=2){
        if(t<n){lo[t]=fmin(lo[t],lo[t+n]);hi[t]=fmax(hi[t],hi[t+n]);sum[t]+=sum[t+n];}
        barrier(CLK_LOCAL_MEM_FENCE);
    }
    if(t==0){int g=3*get_group_id(0);out[g]=lo[0];out[g+1]=hi[0];out[g+2]=sum[0];}
}
"""


class FrameMeter:
    def __init__(self, composer):
        import pyopencl as cl
        self.cl = cl
        devices = [d for p in cl.get_platforms() for d in p.get_devices()
                   if d.type & cl.device_type.GPU and d.double_fp_config and d.max_work_group_size >= 128]
        if not devices:
            raise RuntimeError("No FP64 OpenCL GPU")
        self.context = cl.Context([max(devices, key=lambda d: d.global_mem_size)])
        self.queue = cl.CommandQueue(self.context)
        program = cl.Program(self.context, _SOURCE).build()
        self.compose, self.reduce = program.compose, program.measure
        def constant(values):
            return cl.Buffer(self.context, cl.mem_flags.READ_ONLY | cl.mem_flags.COPY_HOST_PTR,
                             hostbuf=np.ascontiguousarray(values, dtype=np.float64))
        mmr = np.zeros((2, 22), dtype=np.float64)
        for c, (bias, coef) in enumerate(composer.mmr):
            mmr[c, 0] = bias
            mmr[c, 1:1+coef.size] = coef.ravel()
        self.mmr = constant(mmr)
        self.bounds = constant([[b[0], b[-1]] for b in composer.pivots])
        self.lut = constant(composer.luma(np.arange(1024)))
        self.matrix = constant(np.r_[composer.matrix.ravel(), composer.offset, composer.to_bt2020.ravel()])
        self.shape = None

    def stats(self, y, cb, cr, location):
        cl = self.cl
        h, w = y.shape
        count = h*w//4
        groups = (count+127)//128
        if self.shape != (h, w):
            flags = cl.mem_flags
            def buf(size, access):
                return cl.Buffer(self.context, access, size=size)
            self.planes = [buf(v.nbytes, flags.READ_ONLY) for v in (y, cb, cr)]
            self.uv = buf(count*16, flags.READ_WRITE)
            self.output = buf(groups*24, flags.WRITE_ONLY)
            self.shape = (h, w)
        for device, host in zip(self.planes, (y, cb, cr)):
            cl.enqueue_copy(self.queue, device, np.ascontiguousarray(host), is_blocking=True)
        grid = (groups*128,)
        size = (np.int32(w), np.int32(h))
        self.compose(self.queue, grid, (128,), *self.planes, self.uv, self.mmr, self.bounds, *size)
        self.reduce(self.queue, grid, (128,), self.planes[0], self.uv, self.lut, self.matrix, *size,
                    np.float64(.25 if location == "left" else 0.), self.output)
        partial = np.empty((groups, 3), dtype=np.float64)
        cl.enqueue_copy(self.queue, partial, self.output, is_blocking=True)
        result = dict(min_pq=float(partial[:, 0].min()), max_pq=float(partial[:, 1].max()),
                      avg_pq=float(partial[:, 2].sum()/count))
        if not all(0 <= v <= 1 for v in result.values()):
            raise ArithmeticError("Invalid GPU measurements")
        return result


def verified_meter(composer, cpu_measure):
    """Check the active device and its compiler against the reference decoder."""
    meter = FrameMeter(composer)
    rng = np.random.default_rng(8410)
    planes = [rng.integers(0, 1024, shape, dtype=np.uint16) for shape in ((38, 66), (19, 33), (19, 33))]
    for location in ("left", "topleft"):
        ref = cpu_measure(*planes, composer, chroma_location=location)["measured_pq"]
        gpu = meter.stats(*planes, location)
        if any(abs(ref[k] - gpu[k]) > 1e-10 for k in ref):
            raise ArithmeticError("GPU did not match the reference measurement")
    return meter
