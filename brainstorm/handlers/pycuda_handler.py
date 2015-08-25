#!/usr/bin/env python
# coding=utf-8
from __future__ import division, print_function
import numpy as np
import warnings
from pycuda import gpuarray, cumath
import pycuda.driver as drv
import pycuda.autoinit
from pycuda.elementwise import ElementwiseKernel
from pycuda.compiler import SourceModule
import skcuda.linalg as culinalg
import skcuda.misc as cumisc
from brainstorm.handlers.base_handler import Handler
culinalg.init()

try:
    import ctypes
    import libcudnn as cudnn
except ImportError:
    warnings.warn("CUDNN libraries are not available.")


# noinspection PyMethodOverriding
class PyCudaHandler(Handler):

    __undescribed__ = {'context', 'dtype', 'EMPTY'}

    def __init__(self, init_cudnn=False):
        self.context = cumisc._global_cublas_handle
        self.dtype = np.float32
        self.EMPTY = gpuarray.zeros((), dtype=self.dtype)

        if init_cudnn:
            self.cudnn_context = cudnn.cudnnCreate()
            self.cudnn_tensor_format = cudnn.cudnnTensorFormat[
                'CUDNN_TENSOR_NCHW']
            self.cudnn_data_type = cudnn.cudnnDataType[
                'CUDNN_DATA_FLOAT']
            self.cudnn_convmode = cudnn.cudnnConvolutionMode[
                'CUDNN_CROSS_CORRELATION']
            # TODO we should use use PREFER_FASTEST eventually!
            self.cudnn_convpref = cudnn.cudnnConvolutionFwdPreference[
                #'CUDNN_CONVOLUTION_FWD_PREFER_FASTEST']
                'CUDNN_CONVOLUTION_FWD_NO_WORKSPACE']
            self.cudnn_addmode = cudnn.cudnnAddMode['CUDNN_ADD_SAME_C']

    array_type = pycuda.gpuarray.GPUArray
    size = staticmethod(lambda x: x.size)
    shape = staticmethod(lambda x: x.shape)
    reshape = staticmethod(lambda x, s: x.reshape(s))
    slice = staticmethod(lambda x, s: x[s])

    def __init_from_description__(self, description):
        self.__init__()


    def allocate(self, size):
        return gpuarray.zeros(size, dtype=self.dtype)

    @staticmethod
    def fill(mem, val):
        mem.fill(val)

    def set_from_numpy(self, mem, arr):
        assert mem.shape == arr.shape, "Shape of destination ({}) != Shape " \
                                       "of source ({})".format(mem.shape,
                                                               arr.shape)
        mem.set(arr.astype(self.dtype))

    def get_numpy_copy(self, mem):
        assert type(mem) == self.array_type
        return mem.get()

    def create_from_numpy(self, arr):
        return gpuarray.to_gpu(arr.astype(self.dtype))

    @staticmethod
    def copy_to(dest, src):
        # Copy data from src to dest (both must be GPUArrays)
        drv.memcpy_dtod(dest.gpudata, src.gpudata, dest.nbytes)

    def zeros(self, shape):
        return gpuarray.zeros(shape=shape, dtype=self.dtype)

    def ones(self, shape):
        a = self.zeros(shape)
        self.fill(a, 1.0)
        return a

    # ---------------- Mathematical Operations ---------------- #

    def sum_t(self, a, axis, out):
        if len(a.shape) < 3 and (axis == 0 or axis == 1):
            cumisc.sum(a, axis, out)
        elif axis is None:
            self.copy_to(out, cumisc.sum(a))
        else:
            raise NotImplementedError

    @staticmethod
    def dot_mm(a, b, out, transa='N', transb='N'):
        culinalg.dot(a, b, transa=transa, transb=transb, out=out)

    @staticmethod
    def dot_add_mm(a, b, out, transa='N', transb='N'):
        culinalg.add_dot(a, b, out, transa, transb)

    @staticmethod
    def mult_tt(a, b, out):
        mult_kernel(a, b, out)

    @staticmethod
    def mult_add_tt(a, b, out):
        mult_add_kernel(a, b, out)

    @staticmethod
    def mult_st(a, b, out):
        mult_st_kernel(a, b, out)

    @staticmethod
    def add_tt(a, b, out):
        add_mm_kernel(a, b, out)

    @staticmethod
    def add_st(s, t, out):
        add_st_kernel(s, t, out)

    @staticmethod
    def subtract_tt(a, b, out):
        subtract_mm_kernel(a, b, out)

    @staticmethod
    def add_mv(m, v, out):
        cumisc.add_matvec(m, v, out=out)

    @staticmethod
    def broadcast_features_t(a, out):
        assert len(a.shape) == 3
        assert a.shape[2] == 1
        assert len(out.shape) > 2
        a_flat = a.reshape(a.size)
        out_flat = out.reshape(out.size)
        broadcast_features_kernel(out_flat, a_flat, np.prod(out.shape[2:]))

    @staticmethod
    def clip_t(a, a_min, a_max, out):
        clip_kernel(a, out, a_min, a_max)

    @staticmethod
    def log_t(a, out):
        cumath.log(a, out=out)

    @staticmethod
    def divide_tt(a, b, out):
        div_kernel(a, b, out)

    @staticmethod
    def divide_mv(m, v, out):
        """
        Divide (M, N) matrix elementwise by a (1, N) vector using broadcasting.
        """
        cumisc.div_matvec(m, v, out=out)

    @classmethod
    def mult_mv(cls, m, v, out):
        """
        Multiply (M, N) matrix elementwise by a (1, N) vector using
        broadcasting.
        """
        if m.shape == v.shape:
            cls.mult_tt(m, v, out=out)
        else:
            cumisc.mult_matvec(m, v, out=out)

    @staticmethod
    def binarize_v(v, out):
        binarize_v_kernel(out, v, out.shape[0], out.shape[1])

    @staticmethod
    def index_m_by_v(m, v, out):
        index_m_by_v_kernel(out, v, m, m.shape[0], m.shape[1])


    def conv2d_forward_batch(self, inputs, weights, bias, outputs, pad, stride):
        upscalex, upscaley = 1, 1  # currently not exposed to API

        x_desc = cudnn.cudnnCreateTensorDescriptor()
        cudnn.cudnnSetTensor4dDescriptor(x_desc, self.cudnn_tensor_format,
                                         self.cudnn_data_type, *inputs.shape)

        w_desc = cudnn.cudnnCreateFilterDescriptor()
        cudnn.cudnnSetFilter4dDescriptor(w_desc, self.cudnn_data_type, *weights.shape)

        b_desc = cudnn.cudnnCreateTensorDescriptor()
        cudnn.cudnnSetTensor4dDescriptor(b_desc, self.cudnn_tensor_format,
            self.cudnn_data_type, 1, bias.size, 1, 1)

        conv_desc = cudnn.cudnnCreateConvolutionDescriptor()
        cudnn.cudnnSetConvolution2dDescriptor(conv_desc, pad, pad,
            stride[0], stride[1], upscalex, upscaley, self.cudnn_convmode)

        # TODO: remove this sanity check once implementation works
        outshape = cudnn.cudnnGetConvolution2dForwardOutputDim(
            conv_desc, x_desc, w_desc)
        assert(outshape == outputs.shape)
        assert(weights.shape[0] == bias.size)
        assert(outputs.shape[1] == bias.size)

        y_desc = cudnn.cudnnCreateTensorDescriptor()
        cudnn.cudnnSetTensor4dDescriptor(y_desc, self.cudnn_tensor_format,
        self.cudnn_data_type, *outputs.shape)

        # TODO: we hardcode a memory limit of zero for cudnn
        algo = cudnn.cudnnGetConvolutionForwardAlgorithm(
            self.cudnn_context, x_desc, w_desc, conv_desc, y_desc,
            self.cudnn_convpref, 0)

        alpha, beta = 1.0, 1.0
        x_data = ctypes.c_void_p(int(inputs.gpudata))
        w_data = ctypes.c_void_p(int(weights.gpudata))
        b_data = ctypes.c_void_p(int(bias.gpudata))
        y_data = ctypes.c_void_p(int(outputs.gpudata))
        cudnn.cudnnConvolutionForward(self.cudnn_context, alpha, x_desc,
            x_data, w_desc, w_data, conv_desc, algo, None, 0, beta, y_desc,
            y_data)
        cudnn.cudnnAddTensor(self.cudnn_context, self.cudnn_addmode, alpha,
            b_desc, b_data, beta, y_desc, y_data)

        cudnn.cudnnDestroyTensorDescriptor(x_desc)
        cudnn.cudnnDestroyTensorDescriptor(y_desc)
        cudnn.cudnnDestroyFilterDescriptor(w_desc)
        cudnn.cudnnDestroyTensorDescriptor(b_desc)
        cudnn.cudnnDestroyConvolutionDescriptor(conv_desc)
        #cudnn.cudnnDestroy(cudnn_context)

    @staticmethod
    def conv2d_backward_batch(out_deltas, inputs, in_deltas, weights, bias,
                              weight_deltas, bias_deltas, pad, stride):
        upscalex, upscaley = 1, 1  # currently not exposed to API

        x_desc = cudnn.cudnnCreateTensorDescriptor()
        cudnn.cudnnSetTensor4dDescriptor(x_desc, self.cudnn_tensor_format,
            self.cudnn_data_type, *inputs.shape)

        id_desc = cudnn.cudnnCreateTensorDescriptor()
        cudnn.cudnnSetTensor4dDescriptor(id_desc, self.cudnn_tensor_format,
            self.cudnn_data_type, *in_deltas.shape)

        od_desc = cudnn.cudnnCreateTensorDescriptor()
        cudnn.cudnnSetTensor4dDescriptor(od_desc, self.cudnn_tensor_format,
            self.cudnn_data_type, *out_deltas.shape)

        w_desc = cudnn.cudnnCreateFilterDescriptor()
        cudnn.cudnnSetFilter4dDescriptor(w_desc, self.cudnn_data_type,
            *weights.shape)

        dw_desc = cudnn.cudnnCreateFilterDescriptor()
        cudnn.cudnnSetFilter4dDescriptor(dw_desc, self.cudnn_data_type,
            *weight_deltas.shape)

        b_desc = cudnn.cudnnCreateTensorDescriptor()
        cudnn.cudnnSetTensor4dDescriptor(b_desc, self.cudnn_tensor_format,
            self.cudnn_data_type, 1, bias.size, 1, 1)

        db_desc = cudnn.cudnnCreateTensorDescriptor()
        cudnn.cudnnSetTensor4dDescriptor(db_desc, self.cudnn_tensor_format,
            self.cudnn_data_type, 1, bias_deltas.size, 1, 1)

        conv_desc = cudnn.cudnnCreateConvolutionDescriptor()
        cudnn.cudnnSetConvolution2dDescriptor(conv_desc, pad, pad,
            stride[0], stride[1], upscalex, upscaley, self.cudnn_convmode)

        alpha, beta = 1.0, 1.0
        x_data = ctypes.c_void_p(int(inputs.gpudata))
        id_data = ctypes.c_void_p(int(in_deltas.gpudata))
        od_data = ctypes.c_void_p(int(out_deltas.gpudata))
        w_data = ctypes.c_void_p(int(weights.gpudata))
        b_data = ctypes.c_void_p(int(bias.gpudata))
        y_data = ctypes.c_void_p(int(outputs.gpudata))

        cudnn.cudnnConvolutionBackwardFilter(self.cudnn_context, alpha,
            x_desc, x_data, id_desc, id_data, conv_desc, beta, dw_desc, dw_data)

        cudnn.cudnnConvolutionBackwardData(self.cudnn_context, alpha,
            w_desc, w_data, id_desc, id_data, conv_desc, beta, od_desc, od_data)

        cudnn.cudnnConvolutionBackwardBias(self.cudnn_context, alpha,
            od_desc, od_data, beta, db_desc, db_data)

        cudnn.cudnnDestroyTensorDescriptor(x_desc)
        cudnn.cudnnDestroyTensorDescriptor(id_desc)
        cudnn.cudnnDestroyTensorDescriptor(od_desc)
        cudnn.cudnnDestroyFilterDescriptor(w_desc)
        cudnn.cudnnDestroyFilterDescriptor(b_desc)
        cudnn.cudnnDestroyFilterDescriptor(dw_desc)
        cudnn.cudnnDestroyFilterDescriptor(db_desc)
        cudnn.cudnnDestroyConvolutionDescriptor(conv_desc)


    # Activation functions

    @staticmethod
    def sigmoid(x, y):
        sigmoid_kernel(x, y)

    @staticmethod
    def sigmoid_deriv(x, y, dy, dx):
        sigmoid_deriv_kernel(x, y, dy, dx)

    @staticmethod
    def tanh(x, y):
        tanh_kernel(x, y)

    @staticmethod
    def tanh_deriv(x, y, dy, dx):
        tanh_deriv_kernel(x, y, dy, dx)

    @staticmethod
    def rel(x, y):
        rel_kernel(x, y)

    @staticmethod
    def rel_deriv(x, y, dy, dx):
        rel_deriv_kernel(x, y, dy, dx)

    @staticmethod
    def softmax_m(m, out):
        """Applies softmax to matrix over last dimension"""
        n, k = m.shape
        tmp = gpuarray.empty((1, n), dtype=m.dtype)
        _softmax_impl(m, tmp.gpudata, out, np.int32(n),
                      np.int32(k), block=(32, 1, 1), grid=(n, 1, 1))
        return out


mult_kernel = ElementwiseKernel(
    "float* x, float* y, float *out",
    "out[i] = x[i] * y[i]",
    "elem_mult_kernel"
)

mult_add_kernel = ElementwiseKernel(
    "float* x, float* y, float *out",
    "out[i] += x[i] * y[i]",
    "elem_mult_kernel"
)

mult_st_kernel = ElementwiseKernel(
    "float x, float* y, float *out",
    "out[i] = x * y[i]",
    "elem_mult_kernel"
)

add_mm_kernel = ElementwiseKernel(
    "float* x, float* y, float *out",
    "out[i] = x[i] + y[i]",
    "add_mm_kernel"
)

add_st_kernel = ElementwiseKernel(
    "float x, float* y, float *out",
    "out[i] = x + y[i]",
    "add_st_kernel"
)

subtract_mm_kernel = ElementwiseKernel(
    "float* x, float* y, float *out",
    "out[i] = x[i] - y[i]",
    "subtract_mm_kernel"
)

sigmoid_kernel = ElementwiseKernel(
    "float* x, float* y",
    "y[i] = 1.0/(1.0 + exp(-1*x[i]))",
    "sigmoid_kernel"
)

sigmoid_deriv_kernel = ElementwiseKernel(
    "float* x, float* y, float* dy, float* dx",
    "dx[i] = dy[i] * y[i] * (1.0 - y[i])",
    "sigmoid_deriv_kernel"
)

tanh_kernel = ElementwiseKernel(
    "float* x, float* y",
    "y[i] = tanh(x[i])",
    "tanh_kernel"
)

tanh_deriv_kernel = ElementwiseKernel(
    "float* x, float* y, float* dy, float* dx",
    "dx[i] = dy[i] * (1.0 - y[i] * y[i])",
    "tanh_deriv_kernel"
)

rel_kernel = ElementwiseKernel(
    "float* x, float* y",
    "if (x[i]>0) y[i] = x[i]; else y[i]=0.0;",
    "rel_kernel"
)

rel_deriv_kernel = ElementwiseKernel(
    "float* x, float* y, float* dy, float* dx",
    "if (y[i]>0) dx[i] = dy[i]; else dx[i]=0.0;",
    "rel_deriv_kernel"
)

broadcast_features_kernel = ElementwiseKernel(
    "float* out, float* a, unsigned int broadcast_size",
    "out[i] = a[i / broadcast_size]",
    "bc_features_kernel"
)

clip_kernel = ElementwiseKernel(
    "float* a, float* out, float a_min, float a_max",
    "out[i] = fminf(fmaxf(a[i], a_min), a_max);",
    "clip_kernel"
)

div_kernel = ElementwiseKernel(
    "float* a, float* b, float* out",
    "out[i] = a[i] / b[i];",
    "div_kernel"
)

binarize_v_kernel = ElementwiseKernel(
    "float* out, float* v, int nrows, int ncols",
    "out[i] = v[i/ncols] == (i % ncols) ? 1.0f : 0.0f",
    "binarize_v_kernel"
)

index_m_by_v_kernel = ElementwiseKernel(
    "float* out, float* v, float* m, int nrows, int ncols",
    "out[i] = m[i*ncols + int(v[i])]",
    "index_m_by_v_kernel"
)

__softmax_kernel_code = """
    #include "float.h"

    __global__ void softmax_kernel(float* mat, float* tmp, float* out,
                                   unsigned int height, unsigned int width) {
          __shared__ float max_vals[32];
        float cur_max = -FLT_MAX;
        float val = 0;

        for (unsigned int i = threadIdx.x; i < width; i += 32) {
            val = mat[blockIdx.x * width + i];
            if (val > cur_max)
                cur_max = val;
        }

        max_vals[threadIdx.x] = cur_max;
        __syncthreads();
        if (threadIdx.x == 0) {
            cur_max = -FLT_MAX;
            for (unsigned int i = 0; i < 32; i++) {
                if (max_vals[i] > cur_max)
                    cur_max = max_vals[i];
            }
            tmp[blockIdx.x] = cur_max;
        }
        __syncthreads();


        float sum = 0.0;
        for (unsigned int i = threadIdx.x; i < width; i += 32) {
            float x =  __expf(mat[blockIdx.x * width + i] - tmp[blockIdx.x]);
            out[blockIdx.x * width + i] = x;
            sum += x;
        }
        max_vals[threadIdx.x] = sum;
        __syncthreads();
        if (threadIdx.x == 0) {
            sum = 0.0;
            for (unsigned int i = 0; i < 32; i++)
                sum += max_vals[i];
            tmp[blockIdx.x] = sum;
        }
        __syncthreads();
        for (unsigned int i = threadIdx.x; i < width; i += 32) {
            out[blockIdx.x * width + i] /= tmp[blockIdx.x];
        }
    }
    """
_mod = SourceModule(__softmax_kernel_code)
_softmax_impl = _mod.get_function("softmax_kernel")
