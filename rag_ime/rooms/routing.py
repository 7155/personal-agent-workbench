from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence

from rag_ime.agent_definitions import canonical_collaboration_role_id


ROOM_KINDS = frozenset({"collaboration", "roleplay"})
ROOM_ROUTING_POLICIES = frozenset(
    {
        "manual_mentions",
        "moderator",
        "sequential",
        "natural",
        "parallel",
        "invite_only",
        "jev",
    }
)
MAX_ROOM_RESPONDERS = 4

_WORD_PATTERN = re.compile(r"[a-z0-9_+#.-]+|[\u3400-\u9fff]+", re.IGNORECASE)
_RESEARCH_TERMS = frozenset({"查", "搜索", "检索", "调研", "核对", "证据", "资料", "分析", "研究"})
_EXECUTION_TERMS = frozenset({"改", "写", "实现", "执行", "修复", "创建", "删除", "运行", "部署"})
_COORDINATION_TERMS = frozenset({"计划", "规划", "协调", "拆分", "分工", "安排", "汇总"})
_REVIEW_TERMS = frozenset({"审查", "复核", "验收", "检查", "评审", "review"})
# There is deliberately no specialist term set. The other four roles describe a
# working method, so a verb like "审查" is real evidence that the role fits.
# "专业 / 领域 / 专家" describe a subject area instead, and matching them cannot
# tell PostgreSQL tuning from a medical question. Routing on those words would
# manufacture domain fit that nothing in the system can back up. A specialist
# member is still reachable by an explicit @ mention and by descriptor overlap.


def normalize_room_kind(value: object) -> str:
    kind = str(value or "collaboration").strip().lower()
    if kind not in ROOM_KINDS:
        raise ValueError("agent room kind must be collaboration or roleplay")
    return kind


def normalize_routing_policy(value: object) -> str:
    policy = str(value or "natural").strip().lower()
    if policy not in ROOM_ROUTING_POLICIES:
        supported = ", ".join(sorted(ROOM_ROUTING_POLICIES))
        raise ValueError(f"unsupported agent room routing policy; expected one of {supported}")
    return policy


def normalize_routing_config(value: object) -> dict[str, object]:
    source = dict(value) if isinstance(value, Mapping) else {}
    jitter = _float(source.get("naturalJitter"), default=0.04, minimum=0.0, maximum=0.15)
    fallback = str(source.get("fallbackParticipantId") or "").strip()
    return {
        # Natural routing still chooses exactly one responder. Explicit
        # multi-@ fan-out is governed by MAX_ROOM_RESPONDERS separately.
        "maxResponders": 1,
        "naturalJitter": jitter,
        "fallbackParticipantId": fallback,
    }


def plan_room_routes(
    room: Mapping[str, object],
    text: str,
    *,
    requested_participant_ids: Sequence[str] = (),
    profiles: Mapping[str, Mapping[str, object]] | None = None,
    authoritative_participant_id: str = "",
    conversation_only: bool = False,
) -> list[dict[str, object]]:
    """Return deterministic routes for one addressed set or managed fan-out.

    Ordinary conversation is deliberately single-responder, even when a Room
    is configured for parallel managed work. Explicit participant mentions and
    a confirmed WorkItem remain the only ingress paths that can fan out.
    """


    participants = [
        dict(item)
        for item in room.get("participants", [])
        if isinstance(item, Mapping) and item.get("status") == "active"
    ]
    if not participants:
        raise ValueError("agent room has no active participants")
    by_id = {str(item["id"]): item for item in participants}
    policy = normalize_routing_policy(room.get("routingPolicy"))

    requested = list(
        dict.fromkeys(str(value or "").strip() for value in requested_participant_ids)
    )
    requested = [value for value in requested if value]
    selected = [by_id[value] for value in requested if value in by_id]
    if len(selected) != len(requested):
        raise ValueError("invited room participant is unavailable")
    reason = "explicit_invite"
    if not selected:
        selected = _mentioned_participants(text, participants)
        reason = "mention"

    authority_id = str(authoritative_participant_id or "").strip()
    authority = by_id.get(authority_id) if authority_id else None
    if authority_id and authority is None:
        raise ValueError("authoritative room participant is unavailable")
    if authority is not None and policy != "parallel":
        if selected and (
            len(selected) != 1 or str(selected[0]["id"]) != str(authority["id"])
        ):
            raise ValueError(
                "Room participant selection conflicts with the WorkItem current owner"
            )
        decision = _decision(
            room,
            policy,
            authority,
            "work_item_owner",
            participants,
            (),
        )
        return [decision]

    if policy == "parallel" and not (conversation_only and not selected):
        if selected and authority is not None and not any(
            str(item["id"]) == str(authority["id"]) for item in selected
        ):
            raise ValueError(
                "Room participant selection conflicts with the WorkItem current owner"
            )
        if not selected:
            selected = participants
            reason = "parallel"
        if authority is not None:
            selected = [
                authority,
                *(
                    item
                    for item in selected
                    if str(item["id"]) != str(authority["id"])
                ),
            ]

    if not selected:
        return [
            plan_room_route(
                room,
                text,
                requested_participant_ids=(),
                profiles=profiles,
                authoritative_participant_id="",
                conversation_only=conversation_only,
            )
        ]
    if len(selected) > MAX_ROOM_RESPONDERS:
        raise ValueError(
            f"room messages can address at most {MAX_ROOM_RESPONDERS} participants"
        )

    selected_ids = [str(item["id"]) for item in selected]
    decisions: list[dict[str, object]] = []
    for target in selected:
        decision = _decision(room, policy, target, reason, participants, ())
        decision["selectedParticipantIds"] = selected_ids
        decision["dispatchCount"] = len(selected_ids)
        decisions.append(decision)
    return decisions


def plan_room_route(
    room: Mapping[str, object],
    text: str,
    *,
    requested_participant_ids: Sequence[str] = (),
    profiles: Mapping[str, Mapping[str, object]] | None = None,
    authoritative_participant_id: str = "",
    conversation_only: bool = False,
) -> dict[str, object]:
    participants = [
        dict(item)
        for item in room.get("participants", [])
        if isinstance(item, Mapping) and item.get("status") == "active"
    ]
    if not participants:
        raise ValueError("agent room has no active participants")
    by_id = {str(item["id"]): item for item in participants}
    policy = normalize_routing_policy(room.get("routingPolicy"))
    config = normalize_routing_config(room.get("routingConfig"))

    requested = list(dict.fromkeys(str(value or "").strip() for value in requested_participant_ids))
    requested = [value for value in requested if value]
    if len(requested) > 1:
        raise ValueError("plan_room_route accepts one participant; use plan_room_routes for fan-out")
    authority_id = str(authoritative_participant_id or "").strip()
    authority = by_id.get(authority_id) if authority_id else None
    if authority_id and authority is None:
        raise ValueError("authoritative room participant is unavailable")
    if requested:
        target = by_id.get(requested[0])
        if target is None:
            raise ValueError("invited room participant is unavailable")
        if authority is not None and target["id"] != authority["id"]:
            raise ValueError(
                "explicit room participant conflicts with the WorkItem current owner"
            )
        if authority is not None:
            return _decision(
                room,
                policy,
                authority,
                "work_item_owner",
                participants,
                (),
            )
        return _decision(room, policy, target, "explicit_invite", participants, ())

    mentioned = _mentioned_participants(text, participants)
    if len(mentioned) > 1:
        raise ValueError("plan_room_route accepts one participant; use plan_room_routes for fan-out")
    if mentioned:
        if authority is not None and mentioned[0]["id"] != authority["id"]:
            raise ValueError(
                "addressed room participant conflicts with the WorkItem current owner"
            )
        if authority is not None:
            return _decision(
                room,
                policy,
                authority,
                "work_item_owner",
                participants,
                (),
            )
        return _decision(room, policy, mentioned[0], "mention", participants, ())

    if authority is not None:
        return _decision(
            room,
            policy,
            authority,
            "work_item_owner",
            participants,
            (),
        )

    if conversation_only and normalize_room_kind(room.get("roomKind")) == "collaboration":
        facilitator = next(
            (
                participant
                for participant in participants
                if canonical_collaboration_role_id(
                    participant.get("collaborationRole")
                )
                == "coordinator"
            ),
            participants[0],
        )
        return _decision(
            room,
            policy,
            facilitator,
            "facilitator",
            participants,
            (),
        )

    if policy == "parallel":
        if conversation_only:
            return _natural_route_decision(
                room,
                text,
                policy=policy,
                participants=participants,
                profiles=profiles,
                config=config,
            )
        raise ValueError(
            "parallel room routing requires plan_room_routes"
        )

    if policy == "moderator":
        moderator_id = str(room.get("moderatorParticipantId") or "")
        target = by_id.get(moderator_id)
        if target is None:
            raise ValueError("agent room moderator is unavailable")
        return _decision(room, policy, target, "moderator", participants, ())

    if policy == "sequential":
        cursor = max(0, int(room.get("nextSpeakerOrdinal") or 0))
        ordered = sorted(participants, key=lambda item: int(item.get("ordinal") or 0))
        target = next(
            (item for item in ordered if int(item.get("ordinal") or 0) >= cursor),
            ordered[0],
        )
        return _decision(room, policy, target, "sequential", ordered, ())

    return _natural_route_decision(
        room,
        text,
        policy=policy,
        participants=participants,
        profiles=profiles,
        config=config,
    )
def _natural_route_decision(
    room: Mapping[str, object],
    text: str,
    *,
    policy: str,
    participants: Sequence[Mapping[str, object]],
    profiles: Mapping[str, Mapping[str, object]] | None,
    config: Mapping[str, object],
) -> dict[str, object]:
    by_id = {str(item["id"]): item for item in participants}
    profile_values = profiles or {}
    candidates = [
        _natural_candidate(
            room,
            text,
            participant,
            profile_values.get(str(participant["id"]), {}),
            config,
        )
        for participant in participants
    ]
    ranked = sorted(
        candidates,
        key=lambda item: (
            -float(item["score"]),
            int(item["lastSpokeAtMs"] or 0),
            int(item["ordinal"]),
        ),
    )
    selected = ranked[0]
    reason = (
        "descriptor_match"
        if float(selected["score"]) > float(selected["jitter"])
        else "natural_fallback"
    )
    fallback_id = str(config.get("fallbackParticipantId") or "")
    if reason == "natural_fallback" and fallback_id in by_id:
        target = by_id[fallback_id]
        reason = "configured_fallback"
    else:
        target = by_id[str(selected["participantId"])]
    return _decision(room, policy, target, reason, participants, ranked)




def _mentioned_participants(
    text: str,
    participants: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    lowered = str(text).casefold()
    aliases: dict[str, list[dict[str, object]]] = {}
    for value in participants:
        participant = dict(value)
        for alias in {
            str(participant.get("displayName") or "").casefold(),
            str(participant.get("roleId") or "").casefold(),
        }:
            if alias:
                aliases.setdefault(alias, []).append(participant)
    matched: dict[str, dict[str, object]] = {}
    for offset, character in enumerate(lowered):
        if character != "@":
            continue
        candidates = [
            (alias, owners)
            for alias, owners in aliases.items()
            if lowered.startswith(alias, offset + 1)
            and _mention_ends_at_boundary(lowered, offset + 1 + len(alias))
        ]
        if not candidates:
            continue
        longest = max(len(alias) for alias, _owners in candidates)
        owners = {
            str(owner["id"]): owner
            for alias, values in candidates
            if len(alias) == longest
            for owner in values
        }
        if len(owners) != 1:
            raise ValueError("room mention must identify exactly one participant")
        participant = next(iter(owners.values()))
        matched[str(participant["id"])] = participant
    return list(matched.values())


def _natural_candidate(
    room: Mapping[str, object],
    text: str,
    participant: Mapping[str, object],
    profile: Mapping[str, object],
    config: Mapping[str, object],
) -> dict[str, object]:
    query_terms = _terms(text)
    routing_tags = _strings(profile.get("routingTags"))
    traits = _strings(profile.get("traits"))
    descriptors = [
        str(participant.get("displayName") or ""),
        str(participant.get("roleId") or ""),
        str(participant.get("collaborationRole") or ""),
        str(profile.get("tagline") or ""),
        str(profile.get("summary") or ""),
        *routing_tags,
        *traits,
    ]
    descriptor_terms = _terms(" ".join(descriptors))
    overlap = query_terms & descriptor_terms
    tag_hits = [tag for tag in routing_tags if tag.casefold() in str(text).casefold()]
    role = canonical_collaboration_role_id(participant.get("collaborationRole"))
    role_signal = 0.0
    signals: list[str] = []
    if overlap:
        signals.append(f"descriptor:{','.join(sorted(overlap)[:4])}")
    if tag_hits:
        signals.append(f"tag:{','.join(tag_hits[:3])}")
    if role == "researcher" and _contains_any(text, _RESEARCH_TERMS):
        role_signal = 0.24
        signals.append("role:research")
    elif role == "implementer" and _contains_any(text, _EXECUTION_TERMS):
        role_signal = 0.24
        signals.append("role:execution")
    elif role == "coordinator" and _contains_any(text, _COORDINATION_TERMS):
        role_signal = 0.24
        signals.append("role:coordination")
    elif role == "reviewer" and _contains_any(text, _REVIEW_TERMS):
        role_signal = 0.24
        signals.append("role:review")
    lexical = min(0.5, 0.1 * len(overlap))
    tag_score = min(0.35, 0.18 * len(tag_hits))
    jitter_limit = float(config.get("naturalJitter") or 0.0)
    jitter = (
        _stable_fraction(
            str(room.get("id") or ""),
            str(room.get("lastEventSequence") or 0),
            str(participant.get("id") or ""),
            str(text),
        )
        * jitter_limit
        if str(room.get("roomKind") or "collaboration") == "roleplay"
        else 0.0
    )
    score = round(lexical + tag_score + role_signal + jitter, 6)
    return {
        "participantId": str(participant["id"]),
        "displayName": str(participant.get("displayName") or ""),
        "score": score,
        "signals": signals,
        "jitter": round(jitter, 6),
        "lastSpokeAtMs": participant.get("lastSpokeAtMs"),
        "ordinal": int(participant.get("ordinal") or 0),
    }


def _decision(
    room: Mapping[str, object],
    policy: str,
    target: Mapping[str, object],
    reason: str,
    participants: Sequence[Mapping[str, object]],
    candidates: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    public_candidates = [
        {
            "participantId": str(item.get("participantId") or ""),
            "displayName": str(item.get("displayName") or ""),
            "score": float(item.get("score") or 0.0),
            "signals": [str(value) for value in item.get("signals", [])][:6],
        }
        for item in candidates
    ]
    if not public_candidates:
        public_candidates = [
            {
                "participantId": str(value["id"]),
                "displayName": str(value.get("displayName") or ""),
                "score": 1.0 if value["id"] == target["id"] else 0.0,
                "signals": [reason] if value["id"] == target["id"] else [],
            }
            for value in participants
        ]
    return {
        "schemaVersion": "rag-ime.room-route-decision.v1",
        "routingPolicy": policy,
        "roomKind": str(room.get("roomKind") or "collaboration"),
        "selectedParticipantIds": [str(target["id"])],
        "targetParticipantId": str(target["id"]),
        "targetDisplayName": str(target.get("displayName") or ""),
        "reason": reason,
        "candidates": public_candidates,
        "configRevision": int(room.get("configRevision") or 1),
    }


def _terms(value: str) -> set[str]:
    terms: set[str] = set()
    for token in _WORD_PATTERN.findall(str(value).casefold()):
        terms.add(token)
        if token and all("\u3400" <= char <= "\u9fff" for char in token):
            for size in (2, 3):
                terms.update(
                    token[index : index + size]
                    for index in range(max(0, len(token) - size + 1))
                )
    return {term for term in terms if term}


def _strings(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [str(item).strip() for item in value if str(item).strip()][:24]


def _contains_any(text: str, terms: Sequence[str] | frozenset[str]) -> bool:
    return any(term in text for term in terms)


def _stable_fraction(*values: str) -> float:
    digest = hashlib.sha256("\x1f".join(values).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / float(2**64 - 1)


def _mention_ends_at_boundary(text: str, offset: int) -> bool:
    if offset >= len(text):
        return True
    return text[offset].isspace() or text[offset] in "，。！？、,:;；!?)]}】）"


def _float(value: object, *, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))
