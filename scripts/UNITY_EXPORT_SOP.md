# Unity Animation Export SOP

Export Kawaii FBX animations as JSON for the ProjectParrot web VRM player.
Unity's Humanoid system handles all retargeting — the web app just plays the JSON directly.

---

## Prerequisites

- Unity Hub installed (https://unity.com/download)
- Unity Editor version 2021.3+ (any LTS version works)
- The FBX animation files (in `ProjectParrot/web/static/animations/`)

---

## A. Create Unity Project

1. Open **Unity Hub**
2. Click **New Project**
3. Select **3D (Core)** template
4. Name it `ParrotAnimExport`, pick any location
5. Click **Create project** — wait for Unity to open (~1-2 min)

## B. Import FBX Files

1. In Unity's bottom panel, you see the **Project** window with `Assets` folder
2. Right-click on `Assets` → **Create** → **Folder** → name it `Animations`
3. Open your file manager, navigate to `ProjectParrot/web/static/animations/`
4. Select ALL `.fbx` files (Ctrl+A), then **drag them into** Unity's `Assets/Animations` folder
5. Wait for Unity to import (progress bar at bottom — may take 1-2 minutes for 171 files)

## C. Configure FBX Files as Humanoid

**This is the critical step** — tells Unity to use its Humanoid retargeting system.

1. In Unity's Project panel, navigate to `Assets/Animations`
2. Select the **first FBX file** (e.g., `KA_Dash_Bwd.fbx`)
3. In the **Inspector** panel (right side), you see import settings. Click the **Rig** tab
4. Change **Animation Type** from "Generic" to **"Humanoid"**
5. Click **Apply** at the bottom of the Inspector
6. Unity auto-maps the bones. If a dialog says "Avatar is valid" → you're good

Now repeat for ALL FBX files at once:

1. In the Project panel, click the first FBX, then **Shift+click the last FBX** to select ALL
2. In the Inspector, you'll see "Multi-object editing" — click the **Rig** tab
3. Change Animation Type to **"Humanoid"**
4. Click **Apply**
5. Wait for Unity to process all files (progress bar)

## D. Install the Export Script

1. In Unity Project panel, right-click `Assets` → **Create** → **Folder** → name it `Editor`
2. Open file manager, navigate to `ProjectParrot/scripts/`
3. **Drag `unity_export_animations.cs`** into Unity's `Assets/Editor` folder
4. Unity compiles the script (brief progress bar at bottom)

## E. Run the Export

1. In Unity's top menu bar, click **Tools** → **Export Animations to JSON**
2. A small window opens with settings:
   - **Search Folder:** type `Assets/Animations` (where you put the FBX files)
   - **Sample FPS:** leave at `30`
   - **Upper Body Only:** **UNCHECK this** ← important! We need full body (hips + legs)
3. Click **"Find & Export All AnimationClips"**
4. A folder picker dialog appears — navigate to `ProjectParrot/web/static/clips/` and select it
5. Wait for export (progress bar shows each clip — takes ~30 seconds for 171 clips)
6. Dialog says "Exported N clips" → click **OK**

## F. Verify Output

1. Open `ProjectParrot/web/static/clips/` in your file manager
2. You should see 171+ `.json` files (one per animation)
3. Open one (e.g., `KA_Idle01_breathing.json`) and verify:
   - Has `"Hips"` key (not `"mixamorigHips"`)
   - Quaternion values are small numbers (not ±0.707 for Hips — that would indicate wrong coordinate conversion)
   - Has ~20 bone keys per frame (Hips through Feet)

## G. Regenerate Duration Manifest

After export, run from the ProjectParrot root:

```bash
python3 -c "
import json, os
d = 'web/static/clips'
dur = {}
for f in sorted(os.listdir(d)):
    if f.endswith('.json') and f != '_clip_durations.json':
        with open(os.path.join(d, f)) as fh:
            data = json.load(fh)
            dur[data.get('name', f.replace('.json',''))] = data['duration']
with open(os.path.join(d, '_clip_durations.json'), 'w') as fh:
    json.dump(dur, fh, indent=2)
print(f'Wrote {len(dur)} entries to _clip_durations.json')
"
```

## H. Test in Browser

1. Run: `.venv/bin/python web/app.py`
2. Open `localhost:8080`
3. The character should animate with correct arm positions

---

## Troubleshooting

### Arms stuck in T-pose
- Did you **uncheck "Upper Body Only"** in step E? If checked, legs and Hips are missing.

### Character upside down or rotated
- Open a JSON clip and check the Hips quaternion at frame 0
- Should be near `[0, 0, 0, 1]` (identity)
- If it's `[-0.7, 0, -0.7, 0]`, the FBX wasn't configured as Humanoid (step C)

### Poses look slightly off
- Try switching `applyBones()` in `vrm-renderer.js` from `getRawBoneNode()` to `getNormalizedBoneNode()` (line 232)

### "No AnimationClip assets found"
- Make sure **Search Folder** is set to `Assets/Animations` (not just `Assets`)

### "Could not find a Humanoid model"
- At least one FBX must be configured as Humanoid (step C) before running the export
