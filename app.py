"""Honeywell Home T5 thermostat app entry point."""

from homey.app import App


class HoneywellT5App(App):
    async def on_init(self) -> None:
        self.log("Honeywell Home T5 app has been initialized")


homey_export = HoneywellT5App
