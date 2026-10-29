import torch
import torch.nn as nn

from modules.backbones.attention import JointAttention, SplitJointAttention
from modules.backbones.eglu import HalfCacheGLUFFN
from modules.backbones.layers import LayerScale, RMSNorm, GLUFFN, FFN, CgMLP


class PJAC(nn.Module):
    """Parallel Joint Attention + Convolution block for the two-stream setting."""

    def __init__(
            self, dim,
            num_heads,
            head_dim,
            c_kernel_size_token=7,
            m_kernel_size_token=5,
            c_kernel_size_x=31,
            m_kernel_size_x=31,

            c_out_drop_x=0.1,
            c_latent_drop_x=0.0,
            c_out_drop_token=0.1,
            c_latent_drop_token=0.0,

            qk_norm=True,
            use_rope=True,
            theta=10000.0,
            dropout_attn: float = 0.0,
            attn_out_drop_x: float = 0.0,
            attn_out_drop_token: float = 0.0,
            attn_type: str = 'joint',
    ):
        super().__init__()
        if attn_type == 'joint':
            self.jattn = JointAttention(
                dim=dim, num_heads=num_heads,
                qk_norm=qk_norm,
                use_rope=use_rope, theta=theta,
                dropout_attn=dropout_attn, out_drop_x=attn_out_drop_x,
                out_drop_token=attn_out_drop_token, head_dim=head_dim,
            )
        elif attn_type == 'split':
            self.jattn = SplitJointAttention(
                dim=dim, num_heads=num_heads,
                qk_norm=qk_norm,
                use_rope=use_rope, theta=theta,
                dropout_attn=dropout_attn, out_drop_x=attn_out_drop_x,
                out_drop_token=attn_out_drop_token, head_dim=head_dim,
            )
        else:
            raise ValueError(f"Unknown attn_type: {attn_type}")

        self.c_x = CgMLP(
            dim, kernel_size=c_kernel_size_x,
            latent_drop=c_latent_drop_x, out_drop=c_out_drop_x,
        )
        self.c_token = CgMLP(
            dim, kernel_size=c_kernel_size_token,
            latent_drop=c_latent_drop_token, out_drop=c_out_drop_token,
        )

        self.c_norm_x = RMSNorm(dim)
        self.c_norm_token = RMSNorm(dim)

        self.merge_linear_x = nn.Linear(dim * 2, dim)
        self.merge_dw_conv_x = (
            nn.Conv1d(
                dim * 2, dim * 2, kernel_size=m_kernel_size_x, stride=1,
                padding=m_kernel_size_x // 2, groups=dim * 2,
            )
            if m_kernel_size_x != 0 else None
        )
        self.merge_linear_token = nn.Linear(dim * 2, dim)
        self.merge_dw_conv_token = (
            nn.Conv1d(
                dim * 2, dim * 2, kernel_size=m_kernel_size_token, stride=1,
                padding=m_kernel_size_token // 2, groups=dim * 2,
            )
            if m_kernel_size_token != 0 else None
        )

    def forward(self, token, x, t_mask, n_mask):
        a_token, a_x = self.jattn(token, x, t_mask, n_mask)
        c_token, c_x = self.c_token(self.c_norm_token(token)), self.c_x(self.c_norm_x(x))
        m_token, m_x = torch.cat([a_token, c_token], dim=-1), torch.cat([a_x, c_x], dim=-1)
        if self.merge_dw_conv_token is not None:
            m_token = self.merge_dw_conv_token(m_token.transpose(1, 2)).transpose(1, 2) + m_token
        m_token = self.merge_linear_token(m_token)
        if self.merge_dw_conv_x is not None:
            m_x = self.merge_dw_conv_x(m_x.transpose(1, 2)).transpose(1, 2) + m_x
        m_x = self.merge_linear_x(m_x)
        return m_token, m_x


class JEBF(nn.Module):
    """One JEBF layer: FFN_token + FFN_x -> PJAC -> FFN_token + FFN_x.

    Padding is handled via masked_fill (EBF style), no attn_mask.
    """

    def __init__(
            self, dim,
            num_heads,
            head_dim,
            c_kernel_size_token=7,
            m_kernel_size_token=5,
            c_kernel_size_x=31,
            m_kernel_size_x=31,

            c_out_drop_x=0.1,
            c_latent_drop_x=0.0,
            c_out_drop_token=0.1,
            c_latent_drop_token=0.0,

            qk_norm=True,
            use_rope=True,
            theta=10000.0,
            dropout_attn: float = 0.0,
            attn_out_drop_x: float = 0.0,
            attn_out_drop_token: float = 0.0,
            skip_first_ffn=False, skip_out_ffn=False,
            use_ls=True, ffn_type='glu', ffn_latent_drop=0.1, ffn_out_drop=0.1,
            attn_type: str = 'joint',
    ):
        super().__init__()
        self.skip_first_ffn = skip_first_ffn
        self.skip_out_ffn = skip_out_ffn

        if ffn_type == 'glu':
            if not skip_first_ffn:
                self.ffn1_x = GLUFFN(
                    dim, latent_dim=dim * 4, dropout_latent=ffn_latent_drop,
                    dropout_output=ffn_out_drop,
                )
                self.ffn1_token = GLUFFN(
                    dim, latent_dim=dim * 4, dropout_latent=ffn_latent_drop,
                    dropout_output=ffn_out_drop,
                )
            if not skip_out_ffn:
                self.ffn2_x = GLUFFN(
                    dim, latent_dim=dim * 4, dropout_latent=ffn_latent_drop,
                    dropout_output=ffn_out_drop,
                )
                self.ffn2_token = GLUFFN(
                    dim, latent_dim=dim * 4, dropout_latent=ffn_latent_drop,
                    dropout_output=ffn_out_drop,
                )
        elif ffn_type == 'ffn':
            if not skip_first_ffn:
                self.ffn1_x = FFN(
                    dim, latent_dim=dim * 4,
                    dropout_latent=ffn_latent_drop, dropout_output=ffn_out_drop,
                )
                self.ffn1_token = FFN(
                    dim, latent_dim=dim * 4,
                    dropout_latent=ffn_latent_drop, dropout_output=ffn_out_drop,
                )
            if not skip_out_ffn:
                self.ffn2_x = FFN(
                    dim, latent_dim=dim * 4,
                    dropout_latent=ffn_latent_drop, dropout_output=ffn_out_drop,
                )
                self.ffn2_token = FFN(
                    dim, latent_dim=dim * 4,
                    dropout_latent=ffn_latent_drop, dropout_output=ffn_out_drop,
                )
        elif ffn_type == 'cgmlp':
            if not skip_first_ffn:
                self.ffn1_x = CgMLP(
                    dim, latent_dim=int(dim * 2.5), latent_drop=ffn_latent_drop,
                    out_drop=ffn_out_drop, kernel_size=21,
                )
                self.ffn1_token = CgMLP(
                    dim, latent_dim=int(dim * 2.5), latent_drop=ffn_latent_drop,
                    out_drop=ffn_out_drop, kernel_size=21,
                )
            if not skip_out_ffn:
                self.ffn2_x = CgMLP(
                    dim, latent_dim=int(dim * 2.5), latent_drop=ffn_latent_drop,
                    out_drop=ffn_out_drop, kernel_size=7,
                )
                self.ffn2_token = CgMLP(
                    dim, latent_dim=int(dim * 2.5), latent_drop=ffn_latent_drop,
                    out_drop=ffn_out_drop, kernel_size=7,
                )
        elif ffn_type == 'eglu':
            if not skip_first_ffn:
                self.ffn1_x = HalfCacheGLUFFN(d_model=dim, d_ff=dim * 4, gate_type='silu', quant_bits=0, bias=True)
                self.ffn1_token = HalfCacheGLUFFN(d_model=dim, d_ff=dim * 4, gate_type='silu', quant_bits=0, bias=True)
            if not skip_out_ffn:
                self.ffn2_x = HalfCacheGLUFFN(d_model=dim, d_ff=dim * 4, gate_type='silu', quant_bits=0, bias=True)
                self.ffn2_token = HalfCacheGLUFFN(d_model=dim, d_ff=dim * 4, gate_type='silu', quant_bits=0, bias=True)
        else:
            raise ValueError(f"Unknown ffn_type: {ffn_type}")

        self.attn = PJAC(
            dim=dim, num_heads=num_heads, head_dim=head_dim,
            c_kernel_size_token=c_kernel_size_token, m_kernel_size_token=m_kernel_size_token,
            c_kernel_size_x=c_kernel_size_x, m_kernel_size_x=m_kernel_size_x,
            c_out_drop_x=c_out_drop_x, c_latent_drop_x=c_latent_drop_x,
            c_out_drop_token=c_out_drop_token, c_latent_drop_token=c_latent_drop_token,
            qk_norm=qk_norm, use_rope=use_rope, theta=theta,
            dropout_attn=dropout_attn,
            attn_out_drop_x=attn_out_drop_x, attn_out_drop_token=attn_out_drop_token,
            attn_type=attn_type,
        )
        if not skip_first_ffn:
            self.norm_ffn1_x = RMSNorm(dim)
            self.norm_ffn1_token = RMSNorm(dim)
        if not skip_out_ffn:
            self.norm_ffn2_x = RMSNorm(dim)
            self.norm_ffn2_token = RMSNorm(dim)

        if use_ls:
            if not skip_out_ffn:
                self.layer_scale_ffn2_x = LayerScale(dim)
                self.layer_scale_ffn2_token = LayerScale(dim)
            if not skip_first_ffn:
                self.layer_scale_ffn1_x = LayerScale(dim)
                self.layer_scale_ffn1_token = LayerScale(dim)
            self.layer_scale_jpac_x = LayerScale(dim)
            self.layer_scale_jpac_token = LayerScale(dim)
        else:
            if not skip_out_ffn:
                self.layer_scale_ffn2_x = nn.Identity()
                self.layer_scale_ffn2_token = nn.Identity()
            if not skip_first_ffn:
                self.layer_scale_ffn1_x = nn.Identity()
                self.layer_scale_ffn1_token = nn.Identity()
            self.layer_scale_jpac_x = nn.Identity()
            self.layer_scale_jpac_token = nn.Identity()

    def forward(self, token, x, t_mask, n_mask):
        if t_mask is not None:
            x = x.masked_fill(~t_mask.unsqueeze(-1), 0)
        if n_mask is not None:
            token = token.masked_fill(~n_mask.unsqueeze(-1), 0)

        if not self.skip_first_ffn:
            x = self.layer_scale_ffn1_x(self.ffn1_x(self.norm_ffn1_x(x))) + x
            token = self.layer_scale_ffn1_token(self.ffn1_token(self.norm_ffn1_token(token))) + token

        if t_mask is not None:
            x = x.masked_fill(~t_mask.unsqueeze(-1), 0)
        if n_mask is not None:
            token = token.masked_fill(~n_mask.unsqueeze(-1), 0)

        t_o, x_o = self.attn(token, x, t_mask, n_mask)
        x = self.layer_scale_jpac_x(x_o) + x
        token = self.layer_scale_jpac_token(t_o) + token

        if t_mask is not None:
            x = x.masked_fill(~t_mask.unsqueeze(-1), 0)
        if n_mask is not None:
            token = token.masked_fill(~n_mask.unsqueeze(-1), 0)

        if not self.skip_out_ffn:
            x = self.layer_scale_ffn2_x(self.ffn2_x(self.norm_ffn2_x(x))) + x
            token = self.layer_scale_ffn2_token(self.ffn2_token(self.norm_ffn2_token(token))) + token

        return x, token


class JEBFBackbone(nn.Module):
    """Two-stream joint-attention backbone.

    Receives raw spectrogram and pre-embedded tokens, projects both to `dim`,
    then runs them through stacked JEBF layers with joint attention.
    No region-based masking -- padding handled via masked_fill (EBF style).

    Constructor signature: (x_in_dim, token_in_dim, x_out_dim, token_out_dim, **kwargs)
    Forward signature:     forward(x, token, t_mask, n_mask) -> (x_features, token_features)
    """

    def __init__(
            self,
            x_in_dim: int,
            token_in_dim: int,
            x_out_dim: int,
            token_out_dim: int,
            dim: int = 256,
            num_layers: int = 8,
            num_heads: int = 8,
            head_dim: int = 64,

            c_kernel_size_token: int = 7,
            m_kernel_size_token: int = 5,
            c_kernel_size_x: int = 31,
            m_kernel_size_x: int = 31,
            c_out_drop_x: float = 0.1,
            c_latent_drop_x: float = 0.0,
            c_out_drop_token: float = 0.1,
            c_latent_drop_token: float = 0.0,
            qk_norm: bool = True,
            use_rope: bool = True,
            theta: float = 10000.0,
            dropout_attn: float = 0.0,
            attn_out_drop_x: float = 0.0,
            attn_out_drop_token: float = 0.0,
            use_ls: bool = True,
            ffn_type: str = 'glu',
            ffn_latent_drop: float = 0.1,
            ffn_out_drop: float = 0.1,

            use_out_norm: bool = True,
            skip_first_ffn=False,
            skip_out_ffn=False,
            attn_type: str = 'joint',
    ):
        super().__init__()
        self.use_out_norm = use_out_norm
        self.attn_type = attn_type
        self.dim = dim
        self.x_in_dim = x_in_dim
        self.token_in_dim = token_in_dim
        self.x_out_dim = x_out_dim
        self.token_out_dim = token_out_dim

        self.audio_input_proj = nn.Linear(x_in_dim, dim)
        self.text_input_proj = nn.Linear(token_in_dim, dim)

        self.layers = nn.ModuleList([
            JEBF(
                dim=dim, num_heads=num_heads, head_dim=head_dim,
                c_kernel_size_token=c_kernel_size_token, m_kernel_size_token=m_kernel_size_token,
                c_kernel_size_x=c_kernel_size_x, m_kernel_size_x=m_kernel_size_x,
                c_out_drop_x=c_out_drop_x, c_latent_drop_x=c_latent_drop_x,
                c_out_drop_token=c_out_drop_token, c_latent_drop_token=c_latent_drop_token,
                qk_norm=qk_norm, use_rope=use_rope, theta=theta,
                dropout_attn=dropout_attn,
                attn_out_drop_x=attn_out_drop_x, attn_out_drop_token=attn_out_drop_token,
                use_ls=use_ls, ffn_type=ffn_type,
                ffn_latent_drop=ffn_latent_drop, ffn_out_drop=ffn_out_drop,
                skip_first_ffn=skip_first_ffn, skip_out_ffn=skip_out_ffn,
                attn_type=attn_type,
            )
            for _ in range(num_layers)
        ])

        if self.use_out_norm:
            self.output_norm_x = RMSNorm(dim)
            self.output_norm_token = RMSNorm(dim)
        self.output_proj_x = nn.Linear(dim, x_out_dim)
        self.output_proj_token = nn.Linear(dim, token_out_dim)

    def forward(self, x, token, t_mask, n_mask):
        """
        Args:
            x:      [B, T, x_in_dim]      raw spectrogram frames
            token:  [B, N, token_in_dim]  pre-embedded tokens
            t_mask: [B, T]                valid mask for frames
            n_mask: [B, N]                valid mask for tokens
        Returns:
            x_features:     [B, T, x_out_dim]
            token_features: [B, N, token_out_dim]
        """
        x = self.audio_input_proj(x)
        token = self.text_input_proj(token)

        for layer in self.layers:
            x, token = layer(token, x, t_mask, n_mask)

        if self.use_out_norm:
            x = self.output_norm_x(x)
            token = self.output_norm_token(token)

        x_features = self.output_proj_x(x)
        token_features = self.output_proj_token(token)

        return x_features, token_features
