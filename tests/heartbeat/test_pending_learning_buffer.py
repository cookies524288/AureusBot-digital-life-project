"""验证后台语言学习缓冲的容量、去重和批次消费。"""

from dataclasses import dataclass

from aureus.heartbeat.pending_learning_buffer import PendingLearningBuffer


@dataclass(frozen=True)
class Sample:
    key: str


def test_buffer_deduplicates_and_discards_oldest_after_capacity() -> None:
    buffer = PendingLearningBuffer[Sample](lambda sample: sample.key)
    buffer.extend(Sample(str(index)) for index in range(100))
    buffer.extend((Sample("0"), Sample("100")))

    assert len(buffer) == 100
    assert [sample.key for sample in buffer.snapshot()] == [str(index) for index in range(1, 101)]
    assert buffer.dropped_count == 1


def test_buffer_consumes_only_selected_batch_and_keeps_new_samples() -> None:
    buffer = PendingLearningBuffer[Sample](lambda sample: sample.key)
    first, second, third = Sample("first"), Sample("second"), Sample("third")
    buffer.extend((first, second, third))

    buffer.discard((first, third))

    assert buffer.snapshot() == [second]
    assert buffer.dropped_count == 0
