"""
Convert FBX animation files to JSON clip files for the web VRM player.

Uses ufbx (MIT license, no Autodesk code) to parse FBX binary format.

Usage:
    # Single file:
    python scripts/fbx_to_clip_json.py path/to/animation.fbx -o web/static/clips/

    # Batch (entire directory):
    python scripts/fbx_to_clip_json.py path/to/fbx_folder/ -o web/static/clips/

Output format (per clip):
{
  "name": "KA_Combat_BareHands_Combo1",
  "fps": 30,
  "duration": 1.5,
  "frameCount": 45,
  "boneNames": ["Spine", "Chest", ...],
  "frames": [
    {"Spine": [qx,qy,qz,qw], "LeftUpperArm": [qx,qy,qz,qw], ...},
    ...
  ]
}

Dependencies:
    pip install pyufbx   (provides the 'ufbx' module, MIT license)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

import ufbx


# ── Bone name mapping ───────────────────────────────────────────────────────
# Common FBX skeleton bone names → VRM humanoid bone names.
# Covers Mixamo, Unity Humanoid, and many asset store packs.
# Add more mappings as needed for your specific assets.

BONE_MAP: dict[str, str] = {}

# Build case-insensitive lookup from various naming conventions
_VRM_BONES = [
    "Hips", "Spine", "Chest", "UpperChest", "Neck", "Head",
    "LeftShoulder", "RightShoulder",
    "LeftUpperArm", "RightUpperArm",
    "LeftLowerArm", "RightLowerArm",
    "LeftHand", "RightHand",
    "LeftUpperLeg", "RightUpperLeg",
    "LeftLowerLeg", "RightLowerLeg",
    "LeftFoot", "RightFoot",
]

_ALIASES: dict[str, str] = {
    # Mixamo naming
    "mixamorig:Hips": "Hips",
    "mixamorig:Spine": "Spine",
    "mixamorig:Spine1": "Chest",
    "mixamorig:Spine2": "UpperChest",
    "mixamorig:Neck": "Neck",
    "mixamorig:Head": "Head",
    "mixamorig:LeftShoulder": "LeftShoulder",
    "mixamorig:RightShoulder": "RightShoulder",
    "mixamorig:LeftArm": "LeftUpperArm",
    "mixamorig:RightArm": "RightUpperArm",
    "mixamorig:LeftForeArm": "LeftLowerArm",
    "mixamorig:RightForeArm": "RightLowerArm",
    "mixamorig:LeftHand": "LeftHand",
    "mixamorig:RightHand": "RightHand",
    "mixamorig:LeftUpLeg": "LeftUpperLeg",
    "mixamorig:RightUpLeg": "RightUpperLeg",
    "mixamorig:LeftLeg": "LeftLowerLeg",
    "mixamorig:RightLeg": "RightLowerLeg",
    "mixamorig:LeftFoot": "LeftFoot",
    "mixamorig:RightFoot": "RightFoot",
    # Unity Humanoid / common naming
    "Bip01 Spine": "Spine",
    "Bip01 Spine1": "Chest",
    "Bip01 Spine2": "UpperChest",
    "Bip01 Neck": "Neck",
    "Bip01 Head": "Head",
    "Bip01 L Clavicle": "LeftShoulder",
    "Bip01 R Clavicle": "RightShoulder",
    "Bip01 L UpperArm": "LeftUpperArm",
    "Bip01 R UpperArm": "RightUpperArm",
    "Bip01 L Forearm": "LeftLowerArm",
    "Bip01 R Forearm": "RightLowerArm",
    "Bip01 L Hand": "LeftHand",
    "Bip01 R Hand": "RightHand",
    # Kawaii / underscore-separated naming (e.g. "Upper Arm_L", "Shoulder_R")
    "shoulder_l": "LeftShoulder",
    "shoulder_r": "RightShoulder",
    "upper arm_l": "LeftUpperArm",
    "upper arm_r": "RightUpperArm",
    "lower arm_l": "LeftLowerArm",
    "lower arm_r": "RightLowerArm",
    "hand_l": "LeftHand",
    "hand_r": "RightHand",
    "upper leg_l": "LeftUpperLeg",
    "upper leg_r": "RightUpperLeg",
    "lower leg_l": "LeftLowerLeg",
    "lower leg_r": "RightLowerLeg",
    "foot_l": "LeftFoot",
    "foot_r": "RightFoot",
    "upper chest": "UpperChest",
    # Shortened / camelCase variants
    "UpperArm_L": "LeftUpperArm",
    "UpperArm_R": "RightUpperArm",
    "LowerArm_L": "LeftLowerArm",
    "LowerArm_R": "RightLowerArm",
    "Hand_L": "LeftHand",
    "Hand_R": "RightHand",
    # Japanese / anime packs
    "上半身": "Spine",
    "上半身2": "Chest",
    "首": "Neck",
    "頭": "Head",
    "左腕": "LeftUpperArm",
    "右腕": "RightUpperArm",
    "左ひじ": "LeftLowerArm",
    "右ひじ": "RightLowerArm",
    "左手首": "LeftHand",
    "右手首": "RightHand",
}

def _build_bone_map():
    """Build a case-insensitive bone name lookup."""
    # Direct VRM names (exact match)
    for name in _VRM_BONES:
        BONE_MAP[name.lower()] = name
    # Aliases
    for alias, vrm in _ALIASES.items():
        BONE_MAP[alias.lower()] = vrm


_build_bone_map()


def resolve_bone_name(fbx_name: str) -> str | None:
    """Try to map an FBX bone name to a VRM bone name."""
    low = fbx_name.lower()
    # Exact match
    if low in BONE_MAP:
        return BONE_MAP[low]
    # Strip common prefixes (namespace:BoneName, Character:BoneName)
    if ":" in fbx_name:
        suffix = fbx_name.split(":")[-1].lower()
        if suffix in BONE_MAP:
            return BONE_MAP[suffix]
    # Fuzzy: check if any VRM bone name is a substring
    for vrm in _VRM_BONES:
        if vrm.lower() in low:
            return vrm
    return None


# ── Quaternion helpers ──────────────────────────────────────────────────────

def quat_to_list(q) -> list[float]:
    """Convert ufbx Quat to [x, y, z, w] list, with Unity→VRM handedness fix."""
    # FBX (left-handed) → VRM/three.js (right-handed): negate x and y
    return [round(-q.x, 6), round(-q.y, 6), round(q.z, 6), round(q.w, 6)]


def euler_to_quat(rx: float, ry: float, rz: float, order: str = "XYZ") -> list[float]:
    """Convert Euler angles (degrees) to quaternion [x,y,z,w]."""
    rx, ry, rz = math.radians(rx), math.radians(ry), math.radians(rz)
    cx, sx = math.cos(rx / 2), math.sin(rx / 2)
    cy, sy = math.cos(ry / 2), math.sin(ry / 2)
    cz, sz = math.cos(rz / 2), math.sin(rz / 2)
    # XYZ order
    qx = sx * cy * cz + cx * sy * sz
    qy = cx * sy * cz - sx * cy * sz
    qz = cx * cy * sz + sx * sy * cz
    qw = cx * cy * cz - sx * sy * sz
    return [round(-qx, 6), round(-qy, 6), round(qz, 6), round(qw, 6)]


# ── FBX parsing ─────────────────────────────────────────────────────────────

def extract_animations(fbx_path: str, sample_fps: int = 30, upper_body_only: bool = True) -> list[dict]:
    """Parse an FBX file and return a list of animation clip dicts."""
    scene = ufbx.load_file(fbx_path)

    # Collect all bone nodes and their VRM names
    bone_nodes: dict[str, tuple] = {}  # vrm_name → ufbx Node
    for node in scene.nodes:
        vrm_name = resolve_bone_name(node.name)
        if vrm_name:
            bone_nodes[vrm_name] = node

    if not bone_nodes:
        print(f"  WARNING: No recognizable bones in {fbx_path}")
        print(f"  Found nodes: {[n.name for n in scene.nodes[:20]]}")
        return []

    if upper_body_only:
        upper = {
            "Spine", "Chest", "UpperChest", "Neck", "Head",
            "LeftShoulder", "RightShoulder",
            "LeftUpperArm", "RightUpperArm",
            "LeftLowerArm", "RightLowerArm",
            "LeftHand", "RightHand",
        }
        bone_nodes = {k: v for k, v in bone_nodes.items() if k in upper}

    clips = []
    for anim_stack in scene.anim_stacks:
        anim = scene.create_anim(anim_stack)
        duration = anim_stack.time_end - anim_stack.time_begin
        if duration <= 0:
            continue

        frame_count = max(1, int(duration * sample_fps))
        dt = duration / frame_count

        frames = []
        for f in range(frame_count):
            t = anim_stack.time_begin + f * dt
            frame = {}
            for vrm_name, node in bone_nodes.items():
                evaluated = ufbx.evaluate_transform(anim, node, t)
                q = evaluated.rotation
                frame[vrm_name] = quat_to_list(q)
            frames.append(frame)

        clip_name = anim_stack.name or Path(fbx_path).stem
        clips.append({
            "name": clip_name,
            "fps": sample_fps,
            "duration": round(duration, 4),
            "frameCount": frame_count,
            "boneNames": sorted(bone_nodes.keys()),
            "frames": frames,
        })

    return clips


def process_file(fbx_path: str, output_dir: str, fps: int, upper_body: bool) -> int:
    """Process one FBX file, write JSON clips. Returns number of clips exported."""
    print(f"Processing: {fbx_path}")
    try:
        clips = extract_animations(fbx_path, sample_fps=fps, upper_body_only=upper_body)
    except Exception as e:
        print(f"  ERROR: {e}")
        return 0

    for clip in clips:
        safe_name = clip["name"].replace(" ", "_").replace("/", "_").replace("\\", "_")
        out_path = os.path.join(output_dir, f"{safe_name}.json")
        with open(out_path, "w") as f:
            json.dump(clip, f)
        size_kb = os.path.getsize(out_path) / 1024
        print(f"  → {safe_name}.json ({clip['frameCount']} frames, {clip['duration']:.1f}s, {size_kb:.0f}KB)")

    return len(clips)


def main():
    parser = argparse.ArgumentParser(description="Convert FBX animations to JSON for VRM web player")
    parser.add_argument("input", help="FBX file or directory of FBX files")
    parser.add_argument("-o", "--output", default="web/static/clips", help="Output directory (default: web/static/clips)")
    parser.add_argument("--fps", type=int, default=30, help="Sample FPS (default: 30)")
    parser.add_argument("--full-body", action="store_true", help="Export full body (default: upper body only)")
    parser.add_argument("--list-bones", action="store_true", help="Just list bone names found, don't export")
    args = parser.parse_args()

    input_path = Path(args.input)
    os.makedirs(args.output, exist_ok=True)

    if args.list_bones:
        # Just print bone names for debugging the mapping
        if input_path.is_file():
            files = [input_path]
        else:
            files = sorted(input_path.glob("**/*.fbx")) + sorted(input_path.glob("**/*.FBX"))
        for f in files[:3]:
            scene = ufbx.load_file(str(f))
            print(f"\n{f.name} — nodes:")
            for node in scene.nodes:
                vrm = resolve_bone_name(node.name)
                tag = f" → {vrm}" if vrm else ""
                print(f"  {node.name}{tag}")
        return

    if input_path.is_file():
        total = process_file(str(input_path), args.output, args.fps, not args.full_body)
    elif input_path.is_dir():
        fbx_files = sorted(input_path.glob("**/*.fbx")) + sorted(input_path.glob("**/*.FBX"))
        if not fbx_files:
            print(f"No .fbx files found in {input_path}")
            sys.exit(1)
        total = 0
        for f in fbx_files:
            total += process_file(str(f), args.output, args.fps, not args.full_body)
    else:
        print(f"Not found: {input_path}")
        sys.exit(1)

    print(f"\nDone. {total} clips exported to {args.output}/")


if __name__ == "__main__":
    main()
