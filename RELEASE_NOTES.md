# v0.4.31-pre.1 — first public pre-release

RoomControl brings a polished room-by-room panel and an adaptive controller for slow underfloor heating to Home Assistant.

### Highlights

- Glass-style dashboard with live room cards, controller explanations, and reorderable rooms.
- Visual weekly schedules with minute-level switching points and draggable temperature segments.
- Per-room adaptive valve control, outdoor-aware setpoints, and predictive preheating.
- Optional minimum valve opening, outdoor-temperature heating permission, weekly valve exercise, manual override, and probable-open-window indication.
- Persisted learning and operating state across restarts.

### Installation

Download `Engelsoft_RoomControl_0.4.31.zip`, extract it into `/config`, restart Home Assistant, and add **Engelsoft RoomControl** under **Settings → Devices & services**. See the README for setup details.

### Pre-release note

The panel currently uses German. Local automated checks cover the controller and selected integration behavior; operation in a live Home Assistant installation with physical devices has not yet been verified here. Predictive heating is an estimate, and a reported valve entity value does not confirm physical valve movement.
