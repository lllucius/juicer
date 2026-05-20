from __future__ import annotations

from juicer.config import GlobalConfig, SequenceConfig, dump_config_toml


def test_dump_config_toml_writes_only_required_sequence_sounds() -> None:
    config = GlobalConfig(
        boot=SequenceConfig(
            event_start_sound="ignored-start.wav",
            event_stop_sound="startup-complete.wav",
        ),
        shutdown=SequenceConfig(
            event_start_sound="shutdown-start.wav",
            event_stop_sound="ignored-stop.wav",
        ),
    )

    toml = dump_config_toml(config)

    assert 'event_stop_sound = "startup-complete.wav"' in toml
    assert 'event_start_sound = "shutdown-start.wav"' in toml
    assert "ignored-start.wav" not in toml
    assert "ignored-stop.wav" not in toml
