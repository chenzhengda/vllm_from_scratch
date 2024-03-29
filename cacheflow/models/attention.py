from typing import List, Optional

import torch
import torch.nn as nn

from cacheflow import ops
from cacheflow.models import InputMetadata


class OPTCacheFlowAttention(nn.Module):

    def __init__(self, scale: float) -> None:
        super().__init__()
        self.scale = scale

    def _masked_attention(
        self,
        query: torch.Tensor,                        # [num_queries, num_heads, head_size]
        key: torch.Tensor,                          # [num_keys, num_heads, head_size]
        value: torch.Tensor,                        # [num_keys, num_heads, head_size]
        attn_mask: Optional[torch.Tensor] = None,   # [num_queries, num_keys]
    ) -> torch.Tensor:                              # [num_queries, num_heads, head_size]
        query = query * self.scale
        attn = torch.einsum('qhd,khd->hqk', query, key)
        if attn_mask is not None:
            attn = attn + attn_mask
        attn = torch.softmax(attn, dim=-1)
        out = torch.einsum('hqk,khd->qhd', attn, value)
        return out

    def multi_query_kv_attention(
        self,
        output: torch.Tensor,       # [num_prompt_tokens, num_heads, head_size]
        query: torch.Tensor,        # [num_prompt_tokens, num_heads, head_size]
        key: torch.Tensor,          # [num_prompt_tokens, num_heads, head_size]
        value: torch.Tensor,        # [num_prompt_tokens, num_heads, head_size]
        prompt_lens: List[int],
    ) -> None:
        # FIXME(woosuk): Replace the following with a custom op.
        start_idx = 0
        for prompt_len in prompt_lens:
            out = output[start_idx:start_idx + prompt_len]
            q = query[start_idx:start_idx + prompt_len]
            k = key[start_idx:start_idx + prompt_len]
            v = value[start_idx:start_idx + prompt_len]

            attention_mask = torch.triu(
                torch.ones(q.shape[0], k.shape[0]), diagonal=1) * -1e5
            attention_mask = attention_mask.to(dtype=q.dtype, device=q.device)
            attention_out = self._masked_attention(q, k, v, attention_mask)
            out.copy_(attention_out, non_blocking=True)

            start_idx += prompt_len

    def single_query_cached_kv_attention(
        self,
        output: torch.Tensor,           # [num_generation_tokens, num_heads, head_size]
        query: torch.Tensor,            # [num_generation_tokens, num_heads, head_size]
        key_cache: torch.Tensor,        # [num_blocks, num_heads, head_size/x, block_size, x]
        value_cache: torch.Tensor,      # [num_blocks, num_heads, block_size, head_size]
        input_metadata: InputMetadata,
    ) -> None:
        num_heads = value_cache.shape[1]
        head_size = value_cache.shape[3]
        block_size = value_cache.shape[2]
        block_tables = input_metadata.block_tables

        # FIXME(woosuk): Replace the following with a custom op.
        for i in range(input_metadata.num_generation_tokens):
            q = query[i].unsqueeze(0)
            block_table = block_tables[i]
            context_len = int(input_metadata.context_lens[i])

            keys = []
            values = []
            for j in range(context_len):
                block_number = int(block_table[j // block_size])
                block_offset = j % block_size

                k = key_cache[block_number, :, :, block_offset, :]
                k = k.reshape(num_heads, head_size)
                keys.append(k)

                v = value_cache[block_number, :, block_offset, :]
                values.append(v)
            keys = torch.stack(keys, dim=0)
            values = torch.stack(values, dim=0)

            out = self._masked_attention(q, keys, values)
            out = out.view(num_heads, head_size)
            output[i].copy_(out, non_blocking=True)

    def forward(
        self,
        query: torch.Tensor,                    # [num_tokens, num_heads * head_size]
        key: torch.Tensor,                      # [num_tokens, num_heads * head_size]
        value: torch.Tensor,                    # [num_tokens, num_heads * head_size]
        key_cache: torch.Tensor,                # [num_blocks, num_heads, head_size/x, block_size, x]
        value_cache: torch.Tensor,              # [num_blocks, num_heads, block_size, head_size]
        input_metadata: InputMetadata,
        cache_event: Optional[torch.cuda.Event],
    ) -> torch.Tensor:                          # [num_tokens, num_heads * head_size]
        # Pre-allocate the output tensor.
        output = torch.empty_like(query)

        # Prune out paddings if any.
        query = query[:input_metadata.num_valid_tokens]
        key = key[:input_metadata.num_valid_tokens]
        value = value[:input_metadata.num_valid_tokens]

        # Reshape the input tensors.
        num_heads = value_cache.shape[1]
        head_size = value_cache.shape[3]
        query = query.view(-1, num_heads, head_size)
        key = key.view(-1, num_heads, head_size)
        value = value.view(-1, num_heads, head_size)
        output = output.view(-1, num_heads, head_size)

        # Compute the attention op for prompts.
        self.multi_query_kv_attention(
            output, query, key, value, input_metadata.prompt_lens)

        # Wait until the cache op is done.
        if cache_event is not None:
            cache_event.wait()

        # Reshape the keys and values and store them in the cache.
        ops.reshape_and_cache(
            key, value, key_cache, value_cache, input_metadata.slot_mapping)

        if input_metadata.num_generation_tokens > 0:
            # Compute the attention op for generation tokens.
            start_idx = sum(input_metadata.prompt_lens)
            self.single_query_cached_kv_attention(
                output[start_idx:],
                query[start_idx:],
                key_cache,
                value_cache,
                input_metadata)

        # Reshape the output tensor.
        # NOTE(woosuk): The output tensor may include paddings.
        return output.view(-1, num_heads * head_size)
