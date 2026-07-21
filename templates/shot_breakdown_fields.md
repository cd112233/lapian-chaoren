# 打斗拉片字段说明

自动工具只填时间码和参考帧。以下导演/动作/VFX字段需逐镜填写。

## 基础

- `shot_id`
- `source_in` / `source_out` / `duration_sec`
- `transition_in` / `transition_out`
- `narrative_function`

## 摄影

- `shot_scale`
- `camera_height`
- `camera_angle`
- `camera_motion`
- `camera_speed_curve`
- `lens_feel`
- `axis_and_screen_direction`
- `focus_target`

## 动作链

- `actor_a_start_state`
- `actor_b_start_state`
- `dominant_mover`
- `attack_or_defense_intent`
- `attack_trajectory`
- `visible_body_parts`
- `contact_point`
- `contact_time`
- `force_direction`
- `reaction`
- `follow_through`
- `settle_state`
- `result_position`
- `next_shot_bridge`

## 场景与特效

- `scene_layout`
- `foreground_midground_background`
- `light_source`
- `destruction_state`
- `vfx_layers`
- `particles_debris`
- `interactive_light`
- `compositing_notes`

## 声音

- `pre_hit_sound`
- `impact_sound`
- `body_weapon_foley`
- `environment_reaction`
- `music_beat`
- `silence_or_tail`

## 生产路由

- `character_action_complexity`
- `contact_complexity`
- `camera_motion_complexity`
- `recommended_route`
- `required_assets`
- `fallback_route`
- `acceptance_criteria`
