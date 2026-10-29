import torch
import torch.nn as nn


def compute_inv_freq(dim: int, theta: float = 10000.0):
    """pre-compute inv_freq, dim is fixed"""
    return 1.0 / (theta ** (torch.arange(0, dim, 2)[: (dim // 2)].float() / dim))


def compute_freqs_cis_dynamic(x: torch.Tensor, inv_freq: torch.Tensor):
    """ONNX兼容：动态计算cos/sin，序列长度从xa tensor shape获取"""
    seq_len = x.shape[-2]
    t = torch.arange(seq_len, device=x.device, dtype=inv_freq.dtype)
    freqs = torch.outer(t, inv_freq)
    return torch.cos(freqs), torch.sin(freqs)


def single_apply_rotary_emb(
        x: torch.Tensor,
        freqs_cos: torch.Tensor,
        freqs_sin: torch.Tensor,
):
    """ONNX兼容：手动实现复数乘法"""
    x_ = x.float().reshape(*x.shape[:-1], -1, 2).contiguous()
    x_r, x_i = x_[..., 0], x_[..., 1]
    x_out_r = x_r * freqs_cos - x_i * freqs_sin
    x_out_i = x_r * freqs_sin + x_i * freqs_cos
    x_out = torch.stack([x_out_r, x_out_i], dim=-1).flatten(-2)
    return x_out.type_as(x)


class SingleRoPosEmb(nn.Module):
    def __init__(self, dim: int, max_len=5000, theta=10000.0, use_cache=True):
        super().__init__()
        self.dim = dim
        self.theta = theta
        self.use_cache = use_cache
        self.register_buffer('inv_freq', compute_inv_freq(dim, theta), persistent=False)
        if use_cache:
            pe_cos, pe_sin = compute_freqs_cis_dynamic(
                torch.zeros(1, max_len, dim), self.inv_freq)
            self.register_buffer('pe_cos', pe_cos[None, :, :], persistent=False)
            self.register_buffer('pe_sin', pe_sin[None, :, :], persistent=False)

    def extend_pe(self, x):
        if self.pe_cos.size(1) >= x.size(-2):
            return
        pe_cos, pe_sin = compute_freqs_cis_dynamic(x, self.inv_freq)
        self.pe_cos = pe_cos[None, :, :].to(device=x.device)
        self.pe_sin = pe_sin[None, :, :].to(device=x.device)

    def get_pe_dynamic(self, x):
        ndim = x.ndim
        seq_len = x.shape[-2]
        pe_cos, pe_sin = compute_freqs_cis_dynamic(x, self.inv_freq)
        pe_cos = pe_cos.view(*((1,) * (ndim - 2)), seq_len, self.dim // 2)
        pe_sin = pe_sin.view(*((1,) * (ndim - 2)), seq_len, self.dim // 2)
        return pe_cos, pe_sin

    def get_pe_cached(self, x):
        ndim = x.ndim
        seq_len = x.size(-2)
        pe_cos = self.pe_cos[:, :seq_len]
        pe_sin = self.pe_sin[:, :seq_len]
        pe_cos = pe_cos.view(*((1,) * (ndim - 2)), seq_len, self.dim // 2)
        pe_sin = pe_sin.view(*((1,) * (ndim - 2)), seq_len, self.dim // 2)
        return pe_cos, pe_sin

    def forward(self, x):
        if self.use_cache:
            self.extend_pe(x)
            pe_cos, pe_sin = self.get_pe_cached(x)
        else:
            pe_cos, pe_sin = self.get_pe_dynamic(x)
        return single_apply_rotary_emb(x, pe_cos, pe_sin)
