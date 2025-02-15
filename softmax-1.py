# this version is diffusion + thresholding
import math
import logging
from functools import partial
from collections import OrderedDict
from copy import deepcopy
from statistics import mean
 
import torch
import torch.nn as nn
import torch.nn.functional as F
 
from timm.models.layers import PatchEmbed, Mlp, DropPath, trunc_normal_, lecun_normal_
from timm.models.vision_transformer import _init_vit_weights, _load_weights
#from timm.models.vision_transformer import init_weights_vit_timm, _load_weights
from timm.models.helpers import build_model_with_cfg, named_apply, adapt_input_conv
import copy


class Attention(nn.Module):
    def __init__(self, dim, num_heads=8, qkv_bias=False, attn_drop=0., proj_drop=0., layerth=None):
        super().__init__()
        assert dim % num_heads == 0, 'dim should be divisible by num_heads'
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = head_dim ** -0.5

        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)

        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)
        self.alpha = 0.6
        self.layerth = layerth

        self.dim = dim
        # self.g_conv = nn.Conv1d(in_channels=1, out_channels=1, kernel_size=1) # let C=1 be dummy variable here

        self.g_conv = nn.Conv1d(in_channels=self.dim//self.num_heads, out_channels=self.dim//self.num_heads, kernel_size=1)

    def forward(self, x, v0=None):
        B, N, C = x.shape

        qkv = self.qkv(x).reshape(B, N, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
        q, k, v = qkv.unbind(0)   # make torchscript happy (cannot use tensor as tuple)

        attn = (q @ k.transpose(-2, -1)) * self.scale
        attn = attn.softmax(dim=-1)
        attn = self.attn_drop(attn)

        if self.layerth > 0 :
            v0_reshape = v0.reshape(B*self.num_heads, N, C // self.num_heads)
            v0_reshape = v0_reshape.permute(0,2,1) # since Conv1d accept (B,C,N)
            v_reshape = v.reshape(B*self.num_heads, N, C // self.num_heads)
            v_reshape = v_reshape.permute(0,2,1) 

            res = self.alpha * (self.adjoint_conv(self.g_conv, (v0_reshape - self.g_conv(v_reshape))))
            res = res.permute(0,2,1) # back to (B*self.num_heads, N, C // self.num_heads)
            res = res.reshape(B, self.num_heads, N, C // self.num_heads)
        else:
            res = 0

        x = (attn @ v) + res
        x = x.transpose(1, 2).reshape(B, N, C)

        x = self.proj(x)
        x = self.proj_drop(x)

        if self.layerth == 0:
            return x, v
        else:
            return x


    def adjoint_conv(self, conv, input):
        # Reverse the weights and perform convolution
        reversed_weights = torch.flip(conv.weight, dims=[2])
        return nn.functional.conv1d(input, reversed_weights, bias=conv.bias, stride=conv.stride, padding=conv.padding, dilation=conv.dilation)




    
class Block(nn.Module):
 
    def __init__(self, dim, num_heads, mlp_ratio=4., qkv_bias=False, drop=0., attn_drop=0.,
                 drop_path=0., act_layer=nn.GELU, norm_layer=nn.LayerNorm, layerth = None):
        super().__init__()
        self.norm1 = norm_layer(dim)
        self.attn = Attention(dim, num_heads=num_heads, qkv_bias=qkv_bias,
                                    attn_drop=attn_drop, proj_drop=drop, layerth= layerth)
        # NOTE: drop path for stochastic depth, we shall see if this is better than dropout here
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop)
        self.layerth = layerth
 
    def forward(self, x, v0 = None):

        if self.layerth == 0:
            x_, v0 = self.attn(self.norm1(x))
        else:
            x_ = self.attn(self.norm1(x), v0 = v0)

        x = x + self.drop_path(x_)
        
        x = x + self.drop_path(self.mlp(self.norm2(x)))
        if self.layerth == 0:
            return x, v0
        else:
            return x
 
class VisionTransformer(nn.Module):
    """ Vision Transformer
    A PyTorch impl of : `An Image is Worth 16x16 Words: Transformers for Image Recognition at Scale`
        - https://arxiv.org/abs/2010.11929
    Includes distillation token & head support for `DeiT: Data-efficient Image Transformers`
        - https://arxiv.org/abs/2012.12877
    """
 
    def __init__(self, img_size=224, patch_size=16, in_chans=3, num_classes=1000, embed_dim=768, depth=12,
                 num_heads=12, mlp_ratio=4., qkv_bias=True, representation_size=None, distilled=False,
                 drop_rate=0., attn_drop_rate=0., drop_path_rate=0., embed_layer=PatchEmbed, norm_layer=None,
                 act_layer=None, weight_init=''):
        """
        Args:
            img_size (int, tuple): input image size
            patch_size (int, tuple): patch size
            in_chans (int): number of input channels
            num_classes (int): number of classes for classification head
            embed_dim (int): embedding dimension
            depth (int): depth of transformer
            num_heads (int): number of attention heads
            mlp_ratio (int): ratio of mlp hidden dim to embedding dim
            qkv_bias (bool): enable bias for qkv if True
            representation_size (Optional[int]): enable and set representation layer (pre-logits) to this value if set
            distilled (bool): model includes a distillation token and head as in DeiT models
            drop_rate (float): dropout rate
            attn_drop_rate (float): attention dropout rate
            drop_path_rate (float): stochastic depth rate
            embed_layer (nn.Module): patch embedding layer
            norm_layer: (nn.Module): normalization layer
            weight_init: (str): weight init scheme
        """
        super().__init__()
        self.num_classes = num_classes
        self.num_features = self.embed_dim = embed_dim  # num_features for consistency with other models
        self.num_tokens = 2 if distilled else 1
        norm_layer = norm_layer or partial(nn.LayerNorm, eps=1e-6)
        act_layer = act_layer or nn.GELU
       
 
        self.patch_embed = embed_layer(
            img_size=img_size, patch_size=patch_size, in_chans=in_chans, embed_dim=embed_dim)
        num_patches = self.patch_embed.num_patches
 
        self.cls_token = nn.Parameter(torch.zeros(1, 1, embed_dim))
        self.dist_token = nn.Parameter(torch.zeros(1, 1, embed_dim)) if distilled else None
        self.pos_embed = nn.Parameter(torch.zeros(1, num_patches + self.num_tokens, embed_dim))
        self.pos_drop = nn.Dropout(p=drop_rate)
 
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, depth)]  # stochastic depth decay rule
        self.blocks = nn.ModuleList([
            Block(
                dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, drop=drop_rate,
                attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer, act_layer=act_layer, layerth = i)
            for i in range(depth)])
        self.norm = norm_layer(embed_dim)
        self.embed_dim = embed_dim
        self.norm_layer = norm_layer
 
        # Representation layer
        if representation_size and not distilled:
            self.num_features = representation_size
            self.pre_logits = nn.Sequential(OrderedDict([
                ('fc', nn.Linear(embed_dim, representation_size)),
                ('act', nn.Tanh())
            ]))
        else:
            self.pre_logits = nn.Identity()
 
        # Classifier head(s)
        self.head = nn.Linear(self.num_features, num_classes) if num_classes > 0 else nn.Identity()
        self.reverse_head = nn.Linear(num_classes, self.embed_dim) if num_classes > 0 else nn.Identity()

        self.head_dist = None
        if distilled:
            self.head_dist = nn.Linear(self.embed_dim, self.num_classes) if num_classes > 0 else nn.Identity()
 
        self.init_weights(weight_init)
 
    def init_weights(self, mode=''):
        assert mode in ('jax', 'jax_nlhb', 'nlhb', '')
        head_bias = -math.log(self.num_classes) if 'nlhb' in mode else 0.
        trunc_normal_(self.pos_embed, std=.02)
        if self.dist_token is not None:
            trunc_normal_(self.dist_token, std=.02)
        if mode.startswith('jax'):
            # leave cls token as zeros to match jax impl
            named_apply(partial(_init_vit_weights, head_bias=head_bias, jax_impl=True), self)
        else:
            trunc_normal_(self.cls_token, std=.02)
            self.apply(_init_vit_weights)
 
    def _init_weights(self, m):
        # this fn left here for compat with downstream users
        _init_vit_weights(m)
 
    @torch.jit.ignore()
    def load_pretrained(self, checkpoint_path, prefix=''):
        _load_weights(self, checkpoint_path, prefix)
 
    @torch.jit.ignore
    def no_weight_decay(self):
        return {'pos_embed', 'cls_token', 'dist_token'}
 
    def get_classifier(self):
        if self.dist_token is None:
            return self.head
        else:
            return self.head, self.head_dist
 
    def reset_classifier(self, num_classes, global_pool=''):
        self.num_classes = num_classes
        self.head = nn.Linear(self.embed_dim, num_classes) if num_classes > 0 else nn.Identity()
        if self.num_tokens == 2:
            self.head_dist = nn.Linear(self.embed_dim, self.num_classes) if num_classes > 0 else nn.Identity()
        self.norm = self.norm_layer(self.embed_dim)
        
 
    def forward_features(self, x):
        x = self.patch_embed(x)
        cls_token = self.cls_token.expand(x.shape[0], -1, -1)  # stole cls_tokens impl from Phil Wang, thanks
        if self.dist_token is None:
            x = torch.cat((cls_token, x), dim=1)
        else:
            x = torch.cat((cls_token, self.dist_token.expand(x.shape[0], -1, -1), x), dim=1)
        x = self.pos_drop(x + self.pos_embed)
        x, v0 = self.blocks[0](x)
        for i in range(1, 11):
            x = self.blocks[i](x, v0 = v0)
            mid_class_prediction = self.head(self.pre_logits((self.norm(x))[:, 0])) # mid_class_prediction shape : batch * num_classes
            x[:, 0, :] = self.reverse_head(mid_class_prediction)

        x = self.norm(x)
        if self.dist_token is None:
            return self.pre_logits(x[:, 0])
        else:
            return x[:, 0], x[:, 1]
 
    def forward(self, x):
        x = self.forward_features(x)
        if self.head_dist is not None:
            x, x_dist = self.head(x[0]), self.head_dist(x[1])  # x must be a tuple
            if self.training and not torch.jit.is_scripting():
                # during inference, return the average of both classifier predictions
                return x, x_dist
            else:
                return (x + x_dist) / 2
        else:
            x = self.head(x)
        return x


'''
## when layerth = 0, output shape of x,v
##x: torch.Size([1, 16, 64])
##v: torch.Size([1, 8, 16, 8]) shape(B, num_heads, N, C // num_heads)
# v0 should have a shape of (B, N, C // self.num_heads)


def test_attention_block():
    attention = Attention(dim=64, layerth = 0)
    x = torch.randn(1,16,64)  # Batch size of 1, 16 tokens, 64 features
    v0 = torch.randn(1, 8, 16, 8)
    output = attention(x)
    print(output[0].shape) 
    print(output[1].shape) 
    print(torch.sum(output[0]))
    print(torch.sum(output[1]))

def test_block():
    # Define the parameters
    dim = 256
    num_heads = 8
    mlp_ratio = 4.0
    qkv_bias = False
    drop = 0.1
    attn_drop = 0.1
    drop_path = 0.1
    act_layer = nn.GELU
    norm_layer = nn.LayerNorm
    
    # Initialize the Block
    block = Block(dim, num_heads, mlp_ratio, qkv_bias, drop, attn_drop, drop_path, act_layer, norm_layer, layerth=3)
    
    # Create a random input tensor (batch_size, sequence_length, dim)
    x = torch.randn(4, 16, dim)
    v0 = torch.randn(4, 8, 16, 32)  
    # Pass the input through the Block
    output = block(x,v0)
    
    # Print the output shape
    print("Output Shape:", output.shape)



if __name__ == "__main__":
    torch.manual_seed(1)
    from torch.autograd import Variable

    # Create a random input tensor
    batch_size = 8
    img_size = 224
    in_chans = 3
    input_tensor = Variable(torch.randn(batch_size, in_chans, img_size, img_size))

    # Instantiate the VisionTransformer model
    model = VisionTransformer(
        img_size=img_size,
        in_chans=in_chans,
        num_classes=1000,
        embed_dim=768,
        depth=12,
        num_heads=12,
        mlp_ratio=4.
    )

    output = model(input_tensor)

    print(output.shape)
'''



