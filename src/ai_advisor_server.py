#!/usr/bin/env python3
"""Local streaming gateway for the shot-aware AI film advisor.

The review page remains a dependency-free ``file://`` document. This companion
server keeps provider credentials out of the browser, adds the current shot
context to every question, optionally attaches local frame images, and streams
newline-delimited JSON back to the UI.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROMPT = ROOT / "templates" / "film_advisor_system_prompt.md"
DEFAULT_INITIAL_PROMPT = ROOT / "templates" / "initial_breakdown_system_prompt.md"
ALLOWED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
MAX_IMAGE_BYTES = 6 * 1024 * 1024
MAX_IMAGES = 4
VALID_MODES = {"terminology", "camera", "lighting", "color", "grading", "post", "rebuild", "open"}
INITIAL_ANALYSIS_FIELDS = {
    "title",
    "opening_frame_literal",
    "screen_layout",
    "literal_visual_description",
    "end_state",
    "character_positions",
    "clone_count_and_positions",
    "body_part_specifics",
    "timeline_beats",
    "force_contact_response",
    "performance_intent",
    "camera_position",
    "camera_motion",
    "lens_fov",
    "focus_depth",
    "composition_editing",
    "vfx_layers_timing",
    "interactive_lighting",
    "environment_response",
    "sound_beats",
    "next_shot_bridge",
    "often_missed",
    "uncertainties",
}
VALID_CONFIDENCE = {"low", "medium", "high"}
LOCAL_BROWSER_HOSTS = {"127.0.0.1", "localhost", "::1"}


@dataclass(frozen=True)
class AdvisorConfig:
    host: str = "127.0.0.1"
    port: int = 8766
    provider: str = "mock"
    model: str = "film-advisor-mock"
    base_url: str = ""
    api_key: str = ""
    prompt_path: Path = DEFAULT_PROMPT
    initial_prompt_path: Path = DEFAULT_INITIAL_PROMPT
    asset_root: Path | None = None
    timeout_seconds: int = 120
    max_body_bytes: int = 1_000_000

    def public_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "vision_assets": bool(self.asset_root),
            "asset_root_configured": bool(self.asset_root),
            "initial_analysis": True,
        }


def load_system_prompt(path: Path) -> str:
    if not path.exists():
        raise SystemExit(f"missing advisor prompt: {path}")
    prompt = path.read_text(encoding="utf-8").strip()
    if len(prompt) < 200:
        raise SystemExit(f"advisor prompt is unexpectedly short: {path}")
    return prompt


def _clean_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def is_allowed_browser_origin(origin: str | None) -> bool:
    """Allow local review pages while rejecting cross-site localhost requests."""

    if not origin or origin == "null":
        return True
    try:
        parsed = urlparse(origin)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and parsed.hostname in LOCAL_BROWSER_HOSTS
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/"}
        and not parsed.params
        and not parsed.query
        and not parsed.fragment
        and (port is None or 0 < port <= 65535)
    )


def validate_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("request body must be a JSON object")
    question = _clean_text(value.get("question"), 4_000)
    if not question:
        raise ValueError("question is required")
    shot_id = _clean_text(value.get("shot_id"), 32).upper()
    if shot_id and not re.fullmatch(r"SH\d{3,5}", shot_id):
        raise ValueError("shot_id must look like SH001")
    mode = _clean_text(value.get("mode"), 32) or "open"
    if mode not in VALID_MODES:
        mode = "open"
    conversation = value.get("conversation")
    if not isinstance(conversation, list):
        conversation = []
    clean_conversation = []
    for item in conversation[-16:]:
        if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
            continue
        text = _clean_text(item.get("text") or item.get("content"), 8_000)
        if text:
            clean_conversation.append({"role": item["role"], "content": text})
    context = value.get("context") if isinstance(value.get("context"), dict) else {}
    return {
        "request_id": _clean_text(value.get("request_id"), 80) or f"req-{int(time.time() * 1000)}",
        "shot_id": shot_id,
        "mode": mode,
        "question": question,
        "conversation": clean_conversation,
        "context": context,
    }


def validate_initial_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("request body must be a JSON object")
    shot_id = _clean_text(value.get("shot_id"), 32).upper()
    if not re.fullmatch(r"SH\d{3,5}", shot_id):
        raise ValueError("shot_id must look like SH001")
    context = value.get("context") if isinstance(value.get("context"), dict) else {}
    requested = value.get("fields")
    if not isinstance(requested, list):
        requested = sorted(INITIAL_ANALYSIS_FIELDS)
    fields = []
    for field in requested:
        name = _clean_text(field, 80)
        if name in INITIAL_ANALYSIS_FIELDS and name not in fields:
            fields.append(name)
    if not fields:
        raise ValueError("at least one supported analysis field is required")
    memory = value.get("trusted_memory")
    if not isinstance(memory, list):
        memory = []
    clean_memory = []
    for item in memory[:12]:
        if not isinstance(item, dict):
            continue
        clean_memory.append(
            {
                "id": _clean_text(item.get("id"), 80),
                "field": _clean_text(item.get("field"), 80),
                "conclusion": _clean_text(item.get("conclusion"), 2_000),
                "reason": _clean_text(item.get("reason"), 1_000),
                "evidence": _clean_text(item.get("evidence"), 1_000),
            }
        )
    return {
        "request_id": _clean_text(value.get("request_id"), 80) or f"initial-{int(time.time() * 1000)}",
        "shot_id": shot_id,
        "fields": fields,
        "context": context,
        "trusted_memory": clean_memory,
    }


def compact_context(context: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "shot_id",
        "title",
        "phase",
        "source_in_frame",
        "source_out_frame",
        "source_in",
        "source_out",
        "duration_sec",
        "analysis_confidence",
        "boundary_type",
        "evidence",
        "diagnostics",
        "previous_shot",
        "next_shot",
        "assets",
        "trusted_memory",
    }
    result = {key: context[key] for key in allowed if key in context}
    encoded = json.dumps(result, ensure_ascii=False)
    if len(encoded) > 60_000:
        result.pop("diagnostics", None)
        result.pop("previous_shot", None)
        result.pop("next_shot", None)
    return result


def build_messages(payload: dict[str, Any], system_prompt: str) -> list[dict[str, Any]]:
    context = compact_context(payload["context"])
    context_text = json.dumps(context, ensure_ascii=False, indent=2)
    current = (
        f"当前提问模式：{payload['mode']}\n"
        f"当前镜头：{payload['shot_id'] or context.get('shot_id', '未指定')}\n\n"
        "以下是审片页自动附带的结构化证据。它可能包含人工判断，不能代替真实帧图：\n"
        f"```json\n{context_text}\n```\n\n"
        f"用户问题：{payload['question']}"
    )
    return [
        {"role": "system", "content": system_prompt},
        *payload["conversation"],
        {"role": "user", "content": current},
    ]


def build_initial_messages(payload: dict[str, Any], system_prompt: str) -> list[dict[str, Any]]:
    context = compact_context(
        {
            **payload["context"],
            "trusted_memory": payload.get("trusted_memory", []),
        }
    )
    request = {
        "shot_id": payload["shot_id"],
        "requested_fields": payload["fields"],
        "context": context,
    }
    return [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": "请对当前镜头进行第一轮结构化拉片。严格返回 JSON。\n\n"
            + json.dumps(request, ensure_ascii=False, indent=2),
        },
    ]


def _safe_asset_path(root: Path, raw_path: str) -> Path | None:
    if not raw_path or "://" in raw_path or raw_path.startswith("data:"):
        return None
    candidate = (root / raw_path).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    if candidate.suffix.lower() not in ALLOWED_IMAGE_SUFFIXES or not candidate.is_file():
        return None
    if candidate.stat().st_size > MAX_IMAGE_BYTES:
        return None
    return candidate


def collect_visual_assets(payload: dict[str, Any], root: Path | None) -> list[dict[str, str]]:
    if not root:
        return []
    assets = payload.get("context", {}).get("assets", {})
    if not isinstance(assets, dict):
        return []
    selected = []
    for label, key in [
        ("首帧", "start_frame"),
        ("中间帧", "middle_frame"),
        ("尾帧", "end_frame"),
        ("逐帧接触表", "dense_contact_sheet"),
    ]:
        path = _safe_asset_path(root, _clean_text(assets.get(key), 2_000))
        if not path:
            continue
        mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        selected.append(
            {
                "label": label,
                "mime_type": mime_type,
                "data": base64.b64encode(path.read_bytes()).decode("ascii"),
            }
        )
        if len(selected) >= MAX_IMAGES:
            break
    return selected


def _mock_answer(payload: dict[str, Any], image_count: int) -> str:
    question = payload["question"]
    shot_id = payload["shot_id"] or payload.get("context", {}).get("shot_id", "当前镜头")
    evidence = "已附带结构化镜头信息"
    if image_count:
        evidence += f"和 {image_count} 张帧图，但模拟 provider 不执行视觉推理"
    else:
        evidence += "；模拟 provider 未读取帧图，也不执行视觉推理"

    if payload["mode"] in {"camera", "terminology"} or re.search(r"运镜|镜头|靠近|拉远|放大|缩小|跟着|摇|移|变焦|推镜|拉焦", question):
        return f"""### 专业判断
仅凭“画面靠近/变大”不能直接命名。对 {shot_id}，优先需要在 **推镜（dolly in）**、**光学变焦（zoom in）**、**主体自行靠近** 和 **后期裁切放大** 之间判断。以下是术语辨析模板，不是对画面的视觉判断；当前镜头判断为低可信。

### 判断依据
{evidence}。真正的推镜会改变前后景相对位移和透视关系；光学变焦主要改变画面视角，摄影机位置不变；人物自己靠近则环境消失点通常保持稳定。

### 易混概念
如果主体尺寸基本不变、背景却明显压缩或展开，才需要进一步考虑 **变焦推拉（dolly zoom）**。如果只是清晰区域从前景切到后景，那叫 **拉焦/移焦（rack focus）**，不是运镜。

### 建议写法
“画面中主体尺寸逐步增大；暂不能确认来自摄影机前移、光学变焦还是主体靠近。重制前应对比首尾帧的背景透视、地面消失点与前后景视差。”

### 还需确认
主体变大时，背景里的建筑间距和前后景遮挡关系有没有同时变化？"""

    if payload["mode"] == "lighting" or re.search(r"灯|光|照明|阴影|轮廓光|逆光|顶光|亮边", question):
        return f"""### 专业判断
先把它拆成光的功能、方位和质地。你描述的亮边可能是 **侧逆轮廓光（rim / edge light）**，也可能来自场景实景光或 VFX 交互光。模拟 provider 只提供辨析框架，不判断当前画面；结论为低可信。

### 判断依据
{evidence}。轮廓光通常沿头发、肩线或身体外缘形成连续高光，用于把主体从背景中分离；如果亮度随能量特效瞬间增强，则更像交互光。

### 易混概念
逆光描述的是光源相对摄影机和主体的位置；轮廓光描述的是画面中的功能。两者经常同时出现，但不是完全同义。

### 建议写法
“主体由侧后方冷色硬光勾勒头肩外缘，正面以较弱软光保留面部信息；特效峰值期间边缘光同步增强，需核对其是实体灯还是 VFX 交互光。”

### 还需确认
这条亮边是否只出现在靠近特效的一侧，并随特效强弱变化？"""

    if payload["mode"] in {"color", "grading", "post"} or re.search(r"色调|颜色|冷暖|调色|LUT|高级灰|电影感|饱和|对比|黑位|高光", question):
        return f"""### 专业判断
不要先用“电影感”概括。应拆成 **冷暖关系、明度结构、饱和度、对比曲线和高光/黑位处理**。如果是冷紫环境配暖色肤光，可描述为冷暖分离或互补倾向，但不能仅凭外观断言使用了某个 LUT。模拟 provider 不判断当前画面，以下框架为低可信。

### 判断依据
{evidence}。需要分别检查场景本色、灯光色温、白平衡、一级调色和局部选色，避免把综合色偏全部归因于后期。

### 易混概念
“青橙调”是综合色彩关系，不等于固定 LUT；“黑位抬升”是亮度曲线特征，也不等于低对比；bloom、halation 和高光 roll-off 也属于不同层面的处理。

### 建议写法
“整体以低饱和冷紫/青色环境为基底，主体关键肤色或能量高光保留偏暖区域；阴影色彩密度较高，黑位未完全压死，高光采用较柔和的肩部过渡。具体来源需区分灯光、VFX 与调色。”

### 还需确认
关闭或减弱特效层后，主体和背景是否仍保持相同的冷暖分离？"""

    return f"""### 专业判断
这个问题需要先归类到摄影机、构图、灯光、色调、调色、后期质感、动作、剪辑、声音或 VFX 中。当前信息不足，不应强行给单一术语。

### 判断依据
{evidence}，但用户描述还没有说明变化发生在主体、摄影机、光线还是后期画面层。

### 易混概念
同一种视觉结果可能来自现场拍摄、三维渲染、VFX 合成或最终调色，专业描述应先确认来源。

### 建议写法
“当前观察到画面外观发生变化，但其来源待核；需分别检查主体运动、摄影机、灯光、特效层和调色前后版本。”

### 还需确认
你最想命名的是画面的运动方式、光线关系，还是整体颜色和质感？"""


def _chunk_text(text: str, size: int = 28) -> Iterator[str]:
    for index in range(0, len(text), size):
        yield text[index : index + size]


def _extract_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("model response did not contain a JSON object")
    value = json.loads(cleaned[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("model response must be a JSON object")
    return value


def normalize_initial_analysis(
    value: dict[str, Any],
    *,
    shot_id: str,
    requested_fields: list[str],
) -> dict[str, Any]:
    returned_shot = _clean_text(value.get("shot_id"), 32).upper()
    if returned_shot and returned_shot != shot_id:
        raise ValueError(f"model returned {returned_shot}, expected {shot_id}")
    source = value.get("fields")
    if not isinstance(source, dict):
        raise ValueError("model response is missing the fields object")
    fields: dict[str, dict[str, str]] = {}
    for name in requested_fields:
        item = source.get(name)
        if isinstance(item, str):
            item = {"value": item}
        if not isinstance(item, dict):
            continue
        content = _clean_text(item.get("value"), 8_000)
        if not content:
            continue
        confidence = _clean_text(item.get("confidence"), 16).lower()
        if confidence not in VALID_CONFIDENCE:
            confidence = "low"
        evidence = _clean_text(item.get("evidence"), 2_000)
        if not evidence:
            confidence = "low"
        fields[name] = {
            "value": content,
            "confidence": confidence,
            "evidence": evidence,
        }
    if not fields:
        raise ValueError("model response did not contain any supported field values")
    uncertainties = value.get("uncertainties")
    if not isinstance(uncertainties, list):
        uncertainties = []
    memory_used = value.get("memory_used")
    if not isinstance(memory_used, list):
        memory_used = []
    return {
        "shot_id": shot_id,
        "fields": fields,
        "uncertainties": [_clean_text(item, 1_000) for item in uncertainties[:12] if _clean_text(item, 1_000)],
        "memory_used": [_clean_text(item, 80) for item in memory_used[:12] if _clean_text(item, 80)],
    }


def _mock_initial_analysis(payload: dict[str, Any], image_count: int) -> dict[str, Any]:
    shot_id = payload["shot_id"]
    available = payload.get("context", {}).get("evidence", {})
    if not isinstance(available, dict):
        available = {}
    templates = {
        "title": f"模拟初拉 · {shot_id}",
        "opening_frame_literal": "模拟模式未读取连续视频；请根据首帧记录主体、姿态与画面位置。",
        "screen_layout": "模拟模式：需要人工确认前景、中景、后景和主体屏幕位置。",
        "literal_visual_description": "模拟模式只验证人机学习流程，不能代替对当前镜头起因、过程和结果的真实观察。",
        "end_state": "需要回看尾帧与下一镜，确认姿态、力向和能量尾迹。",
        "character_positions": "需要人工确认角色数量、屏幕左右位置与前后景关系。",
        "clone_count_and_positions": "分身数量待核。",
        "body_part_specifics": "需要回看视频，补充具体肢体、支撑点和重心变化。",
        "timeline_beats": "需要按帧标记预备、启动、接触、峰值、回收和余势。",
        "force_contact_response": "接触点、力量来源、受力方向和结果位移待核。",
        "performance_intent": "表演意图待人工结合动作节奏确认。",
        "camera_position": "机位高度、角度与主体关系待核。",
        "camera_motion": "静帧不足以区分固定机位、摄影机位移、主体位移和光学变焦。",
        "lens_fov": "焦段观感与是否发生焦段变化待核，不提供虚构数值。",
        "focus_depth": "需要比较连续帧中的清晰区域，区分拉焦与主体距离变化。",
        "composition_editing": "构图轴线、屏幕方向和切点意图待核。",
        "vfx_layers_timing": "需要回看连续视频，标记 VFX 出现、增强、峰值、交互和消散。",
        "interactive_lighting": "特效交互光是否随能量变化待核。",
        "environment_response": "尘土、碎片、风压和场景破坏待核。",
        "sound_beats": "模拟模式不分析音频；起势、破风、命中和余响待核。",
        "next_shot_bridge": "需要对照下一镜第一帧确认姿态、方向和能量状态。",
        "often_missed": "不要把模拟回答当成画面事实。",
        "uncertainties": "模拟模式仅验证接口与审核流程，全部语义判断仍需人工确认。",
    }
    fields = {}
    for name in payload["fields"]:
        current = _clean_text(available.get(name), 8_000)
        if current and not re.search(r"待核|等待|自动流程", current):
            value = current
            evidence = "沿用页面已有结构化描述；仍需人工核对原片。"
        else:
            value = templates.get(name, "模拟模式没有形成可靠判断。")
            evidence = f"已接收 {image_count} 张帧图；模拟 provider 不执行视觉推理。"
        fields[name] = {"value": value, "confidence": "low", "evidence": evidence}
    return {
        "shot_id": shot_id,
        "fields": fields,
        "uncertainties": ["当前为模拟 provider，不能用于真实镜头结论。"],
        "memory_used": [],
    }


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
        return "".join(parts)
    return ""


def _messages_with_openai_images(
    messages: list[dict[str, Any]], images: list[dict[str, str]]
) -> list[dict[str, Any]]:
    if not images:
        return messages
    result = [dict(message) for message in messages]
    last = dict(result[-1])
    content: list[dict[str, Any]] = [{"type": "text", "text": str(last["content"])}]
    for image in images:
        content.append({"type": "text", "text": f"附图：{image['label']}"})
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{image['mime_type']};base64,{image['data']}"},
            }
        )
    last["content"] = content
    result[-1] = last
    return result


def _provider_url(base_url: str, suffix: str) -> str:
    clean = base_url.rstrip("/")
    return clean if clean.endswith(suffix) else f"{clean}{suffix}"


def iter_openai_compatible(
    config: AdvisorConfig,
    messages: list[dict[str, Any]],
    images: list[dict[str, str]],
) -> Iterator[str]:
    url = _provider_url(config.base_url, "/chat/completions")
    body = json.dumps(
        {"model": config.model, "messages": _messages_with_openai_images(messages, images), "stream": True},
        ensure_ascii=False,
    ).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line == "[DONE]":
                    break
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                choices = event.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                text = _content_text(delta.get("content"))
                if text:
                    yield text
    except urllib.error.HTTPError as error:
        detail = error.read(2_000).decode("utf-8", errors="replace")
        raise RuntimeError(f"provider returned HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"cannot reach provider: {error.reason}") from error


def iter_ollama(
    config: AdvisorConfig,
    messages: list[dict[str, Any]],
    images: list[dict[str, str]],
) -> Iterator[str]:
    ollama_messages = [dict(message) for message in messages]
    if images:
        ollama_messages[-1]["images"] = [image["data"] for image in images]
    url = _provider_url(config.base_url, "/api/chat")
    body = json.dumps(
        {"model": config.model, "messages": ollama_messages, "stream": True},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
            for raw_line in response:
                try:
                    event = json.loads(raw_line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                text = _content_text((event.get("message") or {}).get("content"))
                if text:
                    yield text
                if event.get("done"):
                    break
    except urllib.error.HTTPError as error:
        detail = error.read(2_000).decode("utf-8", errors="replace")
        raise RuntimeError(f"Ollama returned HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"cannot reach Ollama: {error.reason}") from error


def iter_advice(
    config: AdvisorConfig,
    payload: dict[str, Any],
    system_prompt: str,
) -> tuple[Iterable[str], int]:
    images = collect_visual_assets(payload, config.asset_root)
    messages = build_messages(payload, system_prompt)
    if config.provider == "mock":
        return _chunk_text(_mock_answer(payload, len(images))), len(images)
    if config.provider == "openai-compatible":
        return iter_openai_compatible(config, messages, images), len(images)
    if config.provider == "ollama":
        return iter_ollama(config, messages, images), len(images)
    raise RuntimeError(f"unsupported provider: {config.provider}")


def generate_initial_analysis(
    config: AdvisorConfig,
    payload: dict[str, Any],
    system_prompt: str,
) -> tuple[dict[str, Any], int]:
    images = collect_visual_assets(payload, config.asset_root)
    if config.provider == "mock":
        return _mock_initial_analysis(payload, len(images)), len(images)
    messages = build_initial_messages(payload, system_prompt)
    if config.provider == "openai-compatible":
        stream = iter_openai_compatible(config, messages, images)
    elif config.provider == "ollama":
        stream = iter_ollama(config, messages, images)
    else:
        raise RuntimeError(f"unsupported provider: {config.provider}")
    raw = "".join(stream).strip()
    result = normalize_initial_analysis(
        _extract_json_object(raw),
        shot_id=payload["shot_id"],
        requested_fields=payload["fields"],
    )
    return result, len(images)


class AdvisorHTTPServer(ThreadingHTTPServer):
    config: AdvisorConfig
    system_prompt: str
    initial_prompt: str


class AdvisorHandler(BaseHTTPRequestHandler):
    server: AdvisorHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write(f"[{self.log_date_time_string()}] {format % args}\n")

    def _cors_headers(self) -> None:
        origin = self.headers.get("Origin", "")
        if origin and is_allowed_browser_origin(origin):
            self.send_header("Access-Control-Allow-Origin", "null" if origin == "null" else origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def _reject_untrusted_origin(self) -> bool:
        if is_allowed_browser_origin(self.headers.get("Origin")):
            return False
        self._send_json(HTTPStatus.FORBIDDEN, {"error": "browser origin is not allowed"})
        return True

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _write_event(self, payload: dict[str, Any]) -> None:
        line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        self.wfile.write(line)
        self.wfile.flush()

    def do_OPTIONS(self) -> None:  # noqa: N802
        if self._reject_untrusted_origin():
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if self._reject_untrusted_origin():
            return
        if urlparse(self.path).path != "/api/health":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "service": "lapian-ai-film-advisor",
                **self.server.config.public_dict(),
            },
        )

    def do_POST(self) -> None:  # noqa: N802
        if self._reject_untrusted_origin():
            return
        path = urlparse(self.path).path
        if path not in {"/api/advisor", "/api/analyze-shot"}:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > self.server.config.max_body_bytes:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "invalid request size"})
            return
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
            payload = validate_initial_payload(value) if path == "/api/analyze-shot" else validate_payload(value)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return

        if path == "/api/analyze-shot":
            try:
                result, image_count = generate_initial_analysis(
                    self.server.config,
                    payload,
                    self.server.initial_prompt,
                )
            except Exception as error:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(error)[:2_000]})
                return
            self._send_json(
                HTTPStatus.OK,
                {
                    **result,
                    "provider": self.server.config.provider,
                    "model": self.server.config.model,
                    "image_count": image_count,
                    "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
            )
            return

        self.send_response(HTTPStatus.OK)
        self._cors_headers()
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            stream, image_count = iter_advice(
                self.server.config,
                payload,
                self.server.system_prompt,
            )
            self._write_event(
                {
                    "type": "meta",
                    "request_id": payload["request_id"],
                    "provider": self.server.config.provider,
                    "model": self.server.config.model,
                    "image_count": image_count,
                }
            )
            answer_parts = []
            for text in stream:
                if not text:
                    continue
                answer_parts.append(text)
                self._write_event({"type": "delta", "text": text})
            answer = "".join(answer_parts).strip()
            self._write_event({"type": "done", "answer": answer})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as error:  # keep provider details local but actionable
            self._write_event({"type": "error", "message": str(error)[:2_000]})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.getenv("AI_ADVISOR_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("AI_ADVISOR_PORT", "8766")))
    parser.add_argument(
        "--provider",
        choices=["mock", "openai-compatible", "ollama"],
        default=os.getenv("AI_ADVISOR_PROVIDER", "mock"),
    )
    parser.add_argument("--model", default=os.getenv("AI_ADVISOR_MODEL", ""))
    parser.add_argument("--base-url", default=os.getenv("AI_ADVISOR_BASE_URL", ""))
    parser.add_argument("--api-key", default=os.getenv("AI_ADVISOR_API_KEY", ""))
    parser.add_argument("--prompt", type=Path, default=DEFAULT_PROMPT)
    parser.add_argument("--initial-prompt", type=Path, default=DEFAULT_INITIAL_PROMPT)
    parser.add_argument("--asset-root", type=Path, default=os.getenv("AI_ADVISOR_ASSET_ROOT"))
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(os.getenv("AI_ADVISOR_TIMEOUT", "120")),
    )
    return parser.parse_args()


def config_from_args(args: argparse.Namespace) -> AdvisorConfig:
    provider = args.provider
    model = args.model or ("film-advisor-mock" if provider == "mock" else "")
    if not model:
        raise SystemExit("--model or AI_ADVISOR_MODEL is required for a real provider")
    base_url = args.base_url
    if provider == "openai-compatible" and not base_url:
        base_url = "https://api.openai.com/v1"
    if provider == "ollama" and not base_url:
        base_url = "http://127.0.0.1:11434"
    asset_root = Path(args.asset_root).expanduser().resolve() if args.asset_root else None
    if asset_root and not asset_root.is_dir():
        raise SystemExit(f"asset root is not a directory: {asset_root}")
    return AdvisorConfig(
        host=args.host,
        port=args.port,
        provider=provider,
        model=model,
        base_url=base_url,
        api_key=args.api_key,
        prompt_path=Path(args.prompt).expanduser().resolve(),
        initial_prompt_path=Path(args.initial_prompt).expanduser().resolve(),
        asset_root=asset_root,
        timeout_seconds=max(10, args.timeout),
    )


def serve(config: AdvisorConfig) -> None:
    server = AdvisorHTTPServer((config.host, config.port), AdvisorHandler)
    server.config = config
    server.system_prompt = load_system_prompt(config.prompt_path)
    server.initial_prompt = load_system_prompt(config.initial_prompt_path)
    print(
        f"AI film advisor listening on http://{config.host}:{config.port} "
        f"({config.provider} / {config.model})"
    )
    if config.asset_root:
        print(f"Visual asset root: {config.asset_root}")
    else:
        print("Visual assets disabled; answers use text context only.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nAI film advisor stopped.")
    finally:
        server.server_close()


def main() -> int:
    serve(config_from_args(parse_args()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
