from typing import Dict, List, Tuple

from cacheflow.master.block_manager import BlockSpaceManager
from cacheflow.master.frontend import Frontend
from cacheflow.sampling_params import SamplingParams
from cacheflow.sequence import Sequence
from cacheflow.sequence import SequenceGroup
from cacheflow.sequence import SequenceStatus

_MAX_NUM_BATCHED_TOKENS = 2048


class Scheduler:

    def __init__(
        self,
        frontend: Frontend,
        controllers: List,
        block_size: int,
        num_gpu_blocks: int,
        num_cpu_blocks: int,
    ) -> None:
        self.frontend = frontend
        self.controllers = controllers
        self.block_size = block_size
        self.num_gpu_blocks = num_gpu_blocks
        self.num_cpu_blocks = num_cpu_blocks

        # Create the block space manager.
        self.block_manager = BlockSpaceManager(
            block_size=block_size,
            num_gpu_blocks=num_gpu_blocks,
            num_cpu_blocks=num_cpu_blocks,
        )

        # Running sequence groups (FIFO).
        self.running: List[SequenceGroup] = []
        # Mapping: group_id -> num_steps.
        self.num_steps: Dict[int, int] = {}
        # Mapping: group_id -> sampling params.
        self.sampling_params: Dict[int, SamplingParams] = {}

        # Swapped sequence groups (LIFO).
        self.swapped: List[SequenceGroup] = []
        # Pending sequence groups (FIFO).
        self.pending: List[SequenceGroup] = []

    def _fetch_inputs(self) -> None:
        inputs = self.frontend.get_inputs()
        for seq_group, sampling_params in inputs:
            self.pending.append(seq_group)
            self.sampling_params[seq_group.group_id] = sampling_params

    def _free_seq(self, seq: Sequence) -> None:
        seq.status = SequenceStatus.FINISHED
        self.block_manager.free(seq)

    def _allocate(self, seq_group: SequenceGroup) -> None:
        self.block_manager.allocate(seq_group)
        for seq in seq_group.seqs:
            seq.status = SequenceStatus.RUNNING
        self.running.append(seq_group)
        # FIXME(woosuk): Support interactive generation.
        self.num_steps[seq_group.group_id] = 0

    def _append(
        self,
        seq_group: SequenceGroup,
        blocks_to_copy: Dict[int, int],
    ) -> None:
        for seq in seq_group.seqs:
            if seq.status == SequenceStatus.FINISHED:
                continue
            ret = self.block_manager.append(seq)
            if ret is not None:
                src_block, dst_block = ret
                blocks_to_copy[src_block] = dst_block

    def _swap_in(
        self,
        seq_group: SequenceGroup,
        blocks_to_swap_in: Dict[int, int],
    ) -> None:
        mapping = self.block_manager.swap_in(seq_group)
        blocks_to_swap_in.update(mapping)
        for seq in seq_group.seqs:
            if seq.status == SequenceStatus.SWAPPED:
                seq.status = SequenceStatus.RUNNING
        self.running.append(seq_group)

    def _swap_out(
        self,
        seq_group: SequenceGroup,
        blocks_to_swap_out: Dict[int, int],
    ) -> None:
        assert self.block_manager.can_swap_out(seq_group)
        mapping = self.block_manager.swap_out(seq_group)
        blocks_to_swap_out.update(mapping)
        for seq in seq_group.seqs:
            if seq.status == SequenceStatus.RUNNING:
                seq.status = SequenceStatus.SWAPPED
        self.swapped.append(seq_group)

    def step(self) -> None:
        # Blocks that need to be swaped or copied before model execution.
        blocks_to_swap_in: Dict[int, int] = {}
        blocks_to_swap_out: Dict[int, int] = {}
        blocks_to_copy: Dict[int, int] = {}

        # 1. Reserve new slots for the running sequences.
        # NOTE: Here we implicitly assume FCFS scheduling.
        # That is, the most recently added sequence group is the first
        # to be swapped out.
        victim_idx = len(self.running) - 1
        for i, seq_group in enumerate(self.running):
            if i > victim_idx:
                # The i-th sequence group has already been swapped out.
                break
            # OOM. Swap out the victim sequence groups.
            while not self.block_manager.can_append(seq_group):
                victim_seq_group = self.running[victim_idx]
                self._swap_out(victim_seq_group, blocks_to_swap_out)
                victim_idx -= 1
                if i > victim_idx:
                    # No other sequence groups can be swapped out.
                    break
            else:
                self._append(seq_group, blocks_to_copy)
        self.running = self.running[:victim_idx + 1]

        # 2. Swap in the swapped sequences if possible.
        # NOTE: Here we implicitly assume FCFS scheduling.
        # The swapped sequences are in LIFO order.
        for i, seq_group in enumerate(reversed(self.swapped)):
            if self.block_manager.can_swap_in(seq_group):
                self._swap_in(seq_group, blocks_to_swap_in)
                self._append(seq_group, blocks_to_copy)
            else:
                # OOM. Stop swapping.
                self.swapped = self.swapped[:len(self.swapped) - i]
                break
        else:
            # All swapped sequences are swapped in.
            self.swapped.clear()

        num_batched_tokens = sum(
            seq_group.num_seqs(status=SequenceStatus.RUNNING)
            for seq_group in self.running
        )

        # 3. Join new sequences if possible.
        # NOTE: Here we implicitly assume FCFS scheduling.
        # TODO(woosuk): Add a batching policy to control the batch size.
        if not self.swapped:
            # FIXME(woosuk): Acquire a lock to protect pending.
            self._fetch_inputs()
            for i, seq_group in enumerate(self.pending):
                num_prompt_tokens = seq_group.seqs[0].get_len()
                if self.block_manager.can_allocate(seq_group):
                    if (num_batched_tokens + num_prompt_tokens
                        <= _MAX_NUM_BATCHED_TOKENS):
                        self._allocate(seq_group)
                        num_batched_tokens += num_prompt_tokens
                        continue

                self.pending = self.pending[i:]
                break
            else:
                self.pending.clear()

        # Ensure that swap-in and swap-out never happen at the same timestep.
        if blocks_to_swap_in:
            assert not blocks_to_swap_out

        # 4. Create input data structures.
        prompt_tokens: Dict[int, List[int]] = {}
        generation_tokens: Dict[int, int] = {}
        context_lens: Dict[int, int] = {}
        block_tables: Dict[int, List[int]] = {}
        for seq_group in self.running:
            group_id = seq_group.group_id
            num_steps = self.num_steps[group_id]
            # NOTE(woosuk): We assume that the number of steps is 0
            # for the prompt sequences.
            is_prompt = num_steps == 0
            for seq in seq_group.seqs:
                if seq.status != SequenceStatus.RUNNING:
                    continue

                seq_id = seq.seq_id
                block_tables[seq_id] = self.block_manager.get_block_table(seq)
                if is_prompt:
                    prompt_tokens[seq_id] = seq.get_token_ids()
                else:
                    generation_tokens[seq_id] = seq.get_token_ids()[-1]
                    context_lens[seq_id] = seq.get_len()

        # 5. Execute the first stage of the pipeline.
        self.controllers[0].execute_stage(
            prompt_tokens,
            generation_tokens,
            context_lens,
            block_tables,
            blocks_to_swap_in,
            blocks_to_swap_out,
            blocks_to_copy,
        )

    def post_step(
        self,
        next_tokens: Dict[int, Tuple[int, int]],
    ) -> None:
        # Update the running sequences and free blocks.
        for seq_group in self.running:
            group_id = seq_group.group_id
            self.num_steps[group_id] += 1
            stop_token_ids = self.sampling_params[group_id].stop_token_ids

            for seq in seq_group.seqs:
                if seq.status == SequenceStatus.FINISHED:
                    continue

                parent_seq_id, next_token = next_tokens[seq.seq_id]
                if seq.seq_id != parent_seq_id:
                    # The sequence is a fork of the parent sequence (beam search).
                    # Free the current sequence.
                    self.block_manager.free(seq)
                    # Fork the parent sequence.
                    parent_seq = seq_group.find(parent_seq_id)
                    seq.logical_token_blocks = parent_seq.logical_token_blocks.copy()
                    self.block_manager.fork(parent_seq, seq)

                # Append a new token to the sequence.
                seq.append([next_token])

                # Check if the sequence has generated a stop token.
                if next_token in stop_token_ids:
                    self._free_seq(seq)
                    continue

                # Check if the sequence has reached the maximum number of steps.
                max_num_steps = self.sampling_params[group_id].max_num_steps
                if self.num_steps[group_id] == max_num_steps:
                    self._free_seq(seq)
                    continue

        # Update the running sequences.
        running: List[SequenceGroup] = []
        for seq_group in self.running:
            if seq_group.is_finished():
                self._return(seq_group)
            else:
                running.append(seq_group)
        self.running = running

    def _return(self, seq_group: SequenceGroup) -> None:
        group_id = seq_group.group_id
        del self.num_steps[group_id]
        del self.sampling_params[group_id]
        self.frontend.print_response(seq_group)
