# Design System

## Theme

安静的桌面审片工作台。视频监看区保持深色，文字与判断区使用低色度中性灰绿表面；视觉层级由分隔、密度、排版和状态色建立，不依赖阴影或装饰。

## Color

| Role | Token | Value |
|---|---|---|
| Primary text | `--ink` | `#1d2421` |
| Secondary text | `--muted` | `#59645f` |
| Page | `--paper` | `#ecefed` |
| Surface | `--surface` | `#f6f8f6` |
| Navigation rail | `--rail` | `#e5e9e6` |
| Divider | `--line` | `#ccd3cf` |
| Monitor | `--dark` | `#0b0e0f` |
| Primary action | deep green | `#183e34` |
| Active media cue | `--accent` | `#df6737` |
| Error | `--danger` | `#aa3e35` |
| Confirmed | `--success` | `#28765f` |

Standard bright mode may use `#ffffff` surfaces, but the long-review mode is the default. State colors are reserved for selected, warning, error and confirmed information.

## Typography

Use `Inter`, `SF Pro Text`, `PingFang SC`, `Microsoft YaHei`, then `system-ui`. Product labels use one family and a compact fixed scale. Default body text is 14px with 1.68–1.72 line height; user settings allow 13–16px. Monospace is limited to frame numbers, timecodes, IDs and machine-readable formats.

## Layout

The review surface uses a resizable shot rail, a central monitor/workspace and a resizable inspector. The document owns vertical scrolling; rail and inspector remain sticky on desktop. New workflows must use separate project or learning surfaces, tabs, drawers or dialogs instead of permanently narrowing the monitor.

## Components

- Corners stay between 4px and 10px; pills are reserved for compact tags and segmented modes.
- Primary actions use deep green fill; destructive actions use quiet outlined treatment.
- Inputs use visible labels, stable dimensions and a clear focus ring.
- Dialogs contain bounded tasks such as import, export or conflict review; ordinary navigation stays inline.
- Loading states report the current pipeline stage and retain the file/project name.
- Empty states provide one next action without promotional copy.

## Motion

Use 150–200ms state transitions only where they clarify selection, progress or disclosure. Respect `prefers-reduced-motion`; no choreographed page entrance or decorative movement.

## Responsive Behavior

Desktop preserves the three-column review layout. Medium widths hide the inspector behind a deliberate review control. Mobile removes the shot rail, uses a two-row shot toolbar and presents review/learning panels as full-height surfaces. Text and controls must never overlap or depend on hover.

Media geometry is evidence, not decoration. Portrait, square, landscape and ultrawide sources use orientation-specific monitor heights. Direct media is always constrained to the monitor with `object-fit: contain`; contact sheets use letterboxing instead of crop. Ultrawide start/middle/end comparison stacks vertically on desktop, while narrow screens use a natural single-column document flow so every frame remains complete and scrollable.
