"""`SLACK_MODE=console`: Slack simulated in the terminal (the brief allows a mocked Slack).

It behaves like the in-memory fake (taken names, markers, topics) and prints every channel, topic
and message, so the video can show exactly what the Communication Agent would send. In production
this is the real `SlackWorkspace` with Slack Connect (design D8).
"""

from __future__ import annotations

from ..fakes import FakeSlack


class ConsoleSlack(FakeSlack):
    def create_channel(self, name: str, purpose: str) -> str:
        channel_id = super().create_channel(name, purpose)
        print(f"[slack] created #{name} ({channel_id}); purpose: {purpose}")
        return channel_id

    def set_topic(self, channel_id: str, topic: str) -> None:
        super().set_topic(channel_id, topic)
        print(f"[slack] #{self.channels[channel_id]['name']} topic: {topic}")

    def invite(self, channel_id: str, emails: list[str]) -> None:
        super().invite(channel_id, emails)
        print(f"[slack] invited {', '.join(emails)} to #{self.channels[channel_id]['name']}")
        print("[slack] production: invite the customer contact with Slack Connect")

    def post(self, channel: str, text: str, marker: str = "") -> str:
        ts = super().post(channel, text, marker)
        name = self.channels[channel]["name"] if channel in self.channels else channel
        print(
            f"[slack] message to #{name}:\n"
            + "\n".join(f"    {line}" for line in text.splitlines())
        )
        return ts
