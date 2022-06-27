from typing import List
import torch
from torch import nn, BoolTensor, FloatTensor, LongTensor

class GLUTorch(nn.Module):
    def __init__(self, count_in_out, count_middle):
        super().__init__()
        self.gelu = nn.GELU()
        self.ln0 = nn.LayerNorm(count_in_out)
        self.ln1 = nn.LayerNorm(count_middle)
        self.fc0 = nn.Linear(count_in_out, count_middle, bias=False)
        self.fc1 = nn.Linear(count_in_out, count_middle, bias=False)
        self.fc2 = nn.Linear(count_middle, count_in_out, bias=False)
    
    def forward(self, z: FloatTensor) -> FloatTensor:
        z = self.ln0.forward(z)
        w = self.fc0.forward(z)
        w = self.gelu.forward(w)
        v = self.fc1.forward(z)
        z = self.ln1.forward(w * v)
        z = self.fc2.forward(z)
        return z

class AttentionTorch(nn.Module):
    def __init__(self, head_count: int, embed_count: int):
        super().__init__()
        self.head_count = head_count
        self.embed_count = embed_count
        self.head_dim = embed_count // head_count

        self.k_proj = nn.Conv2d(embed_count, embed_count, 1, bias=False)
        self.v_proj = nn.Conv2d(embed_count, embed_count, 1, bias=False)
        self.q_proj = nn.Conv2d(embed_count, embed_count, 1, bias=False)
        self.out_proj = nn.Conv2d(embed_count, embed_count, 1, bias=False)
    
    def forward(self,
        keys: FloatTensor,
        values: FloatTensor,
        queries: FloatTensor,
        attention_mask: BoolTensor
    ) -> FloatTensor:
        batch_count = keys.shape[0]

        # b(hc)1q -> bqhc
        # print(keys.shape, "keys", values.shape, "values", queries.shape, "queries")
        keys = keys.transpose(1, 3)
        keys = keys.reshape(keys.shape[:2] + (self.head_count, -1))

        # b(hc)1q -> bchq
        shape = (batch_count, self.head_count, self.head_dim, -1)
        values = values.reshape(shape)
        values = values.transpose(1, 2)
        queries = queries.reshape(shape)
        queries = queries.transpose(1, 2)

        # print(keys.shape, "keys", values.shape, "values", queries.shape, "queries")

        attention_bias = torch.where(
            attention_mask,
            torch.zeros([1, 1]),
            torch.ones([1, 1]) * (-torch.inf),
        )
        attention_weights: FloatTensor = torch.einsum(
            'bchq,bkhc->bkhq',
            queries / self.head_dim ** 0.5, 
            keys
        )
        attention_weights += attention_bias[:, :, None, None]
        attention_weights = torch.softmax(attention_weights, 1)
        # print(attention_weights.shape, "attention_weights")
        hidden_state: FloatTensor = torch.einsum(
            "bkhq,bchk->bchq",
            attention_weights, 
            values
        )
        # bchq -> b(hc)1q
        # print(hidden_state.shape, "hidden_state")
        hidden_state = hidden_state.transpose(1, 2)
        hidden_state = hidden_state.reshape(batch_count, self.embed_count, 1, -1)
        hidden_state = self.out_proj.forward(hidden_state)
        # print(hidden_state.shape, "hidden_state")
        return hidden_state


class EncoderSelfAttentionTorch(AttentionTorch):
    def forward(
        self,
        encoder_state: FloatTensor,
        attention_mask: BoolTensor
    ) -> FloatTensor:
        encoder_state = encoder_state.transpose(1, 2).unsqueeze(2)
        # print(encoder_state.shape, "encoder_state")
        keys = self.k_proj.forward(encoder_state)
        values = self.v_proj.forward(encoder_state)
        queries = self.q_proj.forward(encoder_state)
        return super().forward(keys, values, queries, attention_mask)


class EncoderLayerTorch(nn.Module):
    def __init__(self, embed_count: int, head_count: int, glu_embed_count: int):
        super().__init__()
        self.pre_self_attn_layer_norm = nn.LayerNorm(embed_count)
        self.self_attn = EncoderSelfAttentionTorch(head_count, embed_count)
        self.self_attn_layer_norm = nn.LayerNorm(embed_count)
        self.glu = GLUTorch(embed_count, glu_embed_count)
    
    def forward(
        self,
        encoder_state: FloatTensor,
        attention_mask: BoolTensor
    ) -> FloatTensor:
        residual = encoder_state
        encoder_state = self.pre_self_attn_layer_norm.forward(encoder_state)
        encoder_state = self.self_attn.forward(encoder_state, attention_mask)
        encoder_state = encoder_state.transpose(1, 3).squeeze(2)
        encoder_state = self.self_attn_layer_norm.forward(encoder_state)
        encoder_state = residual + encoder_state
        residual = encoder_state
        encoder_state = self.glu.forward(encoder_state)
        encoder_state = residual + encoder_state
        return encoder_state


class DalleBartEncoderTorch(nn.Module):
    def __init__(self,
        layer_count: int,
        embed_count: int,
        attention_head_count: int,
        text_vocab_count: int,
        text_token_count: int,
        glu_embed_count: int
    ):
        super().__init__()
        self.embed_tokens = nn.Embedding(text_vocab_count, embed_count)
        self.embed_positions = nn.Embedding(text_token_count, embed_count)
        self.layers: List[EncoderLayerTorch] = nn.ModuleList([
            EncoderLayerTorch(
                embed_count = embed_count,
                head_count = attention_head_count,
                glu_embed_count = glu_embed_count
            ) 
            for _ in range(layer_count)
        ])
        self.layernorm_embedding = nn.LayerNorm(embed_count)
        self.final_ln = nn.LayerNorm(embed_count)

    def forward(self, text_tokens: LongTensor) -> FloatTensor:
        attention_mask = text_tokens.not_equal(1)
        batch_count, token_count = text_tokens.shape
        pose_tokens = torch.stack([torch.arange(token_count)] * batch_count)
        encoder_state = (
            self.embed_tokens.forward(text_tokens) +
            self.embed_positions.forward(pose_tokens)
        )
        encoder_state = self.layernorm_embedding.forward(encoder_state)
        for layer in self.layers:
            encoder_state = layer.forward(encoder_state, attention_mask)
        encoder_state = self.final_ln.forward(encoder_state)
        return encoder_state