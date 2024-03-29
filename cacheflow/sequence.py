import enum
from typing import List, Optional

from cacheflow.block import LogicalTokenBlock


class SequenceStatus(enum.Enum):
    PENDING = enum.auto()
    RUNNING = enum.auto()
    SWAPPED = enum.auto()
    FINISHED = enum.auto()


class Sequence:

    def __init__(
        self,
        seq_id: int,
        token_ids: List[int],
        block_size: int,
    ) -> None:
        self.seq_id = seq_id
        self.block_size = block_size

        self.logical_token_blocks: List[LogicalTokenBlock] = []
        # Initialize the logical token blocks with the given token ids.
        self.append(token_ids)

        self.status = SequenceStatus.PENDING

    def add_block(self) -> None:
        block = LogicalTokenBlock(
            block_number=len(self.logical_token_blocks),
            block_size=self.block_size,
        )
        self.logical_token_blocks.append(block)

    def append(self, token_ids: List[int]) -> None:
        while token_ids:
            if not self.logical_token_blocks:
                self.add_block()

            last_block = self.logical_token_blocks[-1]
            if last_block.is_full():
                self.add_block()
                last_block = self.logical_token_blocks[-1]

            num_empty_slots = last_block.get_num_empty_slots()
            last_block.append(token_ids[:num_empty_slots])
            token_ids = token_ids[num_empty_slots:]

    def get_len(self) -> int:
        return sum(block.num_tokens for block in self.logical_token_blocks)

    def get_token_ids(self) -> List[int]:
        token_ids: List[int] = []
        for block in self.logical_token_blocks:
            token_ids.extend(block.get_token_ids())
        return token_ids

    def __repr__(self) -> str:
        return (f'Sequence(seq_id={self.seq_id}, '
                f'status={self.status.name}, '
                f'num_blocks={len(self.logical_token_blocks)})')


class SequenceGroup:

    def __init__(
        self,
        group_id: int,
        seqs: List[Sequence],
    ) -> None:
        self.group_id = group_id
        self.seqs = seqs

    def num_seqs(self, status: Optional[SequenceStatus] = None) -> int:
        if status is None:
            return len(self.seqs)
        else:
            return len([seq for seq in self.seqs if seq.status == status])

    def find(self, seq_id: int) -> Sequence:
        for seq in self.seqs:
            if seq.seq_id == seq_id:
                return seq
        raise ValueError(f'Sequence {seq_id} not found.')

    def is_finished(self) -> bool:
        return all(seq.status == SequenceStatus.FINISHED for seq in self.seqs)

    def __repr__(self) -> str:
        return (f'SequenceGroup(group_id={self.group_id}, '
                f'num_seqs={len(self.seqs)})')
