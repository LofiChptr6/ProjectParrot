> Historical local research notes. Motion scripts, clips, Blender scenes, and preview movies referenced below remain local experiments and are not included in the facial-only release. See [current release scope](../../../docs/character-expression-and-motion-work.md).

# Mocha: scenario gestures and facial variations

Revision 2 contains **14 draft original scenario gestures** and **12 new facial
choices**. Read [SCENARIOS.md](SCENARIOS.md) for conversational situations,
movement beats, and suggested expressions. The original 171 purchased motions
remain available. This revision contains no softened copies of purchased clips.

**Motion quality is not accepted.** Complete welcome/shy comparisons show
insufficient lower-body acting. The draft catalog additions have been withdrawn;
the clips remain available in rehearsal. See [REVIEW.md](REVIEW.md) and
[complete comparison movies](motion-review.html). Facial changes remain enabled.

Examples: a bashful response to a compliment, a warm welcome, a hug offer,
hand-to-heart reassurance, a blown kiss, playful mock offense, a sleepy
goodnight, and quiet attentive company. Each gesture has an entry, readable
pose, and release. Feet stay planted. The heart gesture is a small framed
hand pose, not a precisely closed finger-heart silhouette.

## Review

From ProjectParrot:

```powershell
python character/motion_packs/soft_kawaii/tools/preview_server.py
```

Open <http://127.0.0.1:8766>. Select a scenario draft, pause/scrub, change the face
and strength, or compare the original and revised VRM. Record complete take
saves real-time video and sampled frames in `.cache/motion_review`.
Fit full body shows hands and feet using an inspection camera. Face view and orbit are
inspection controls; Website framing restores the live site's camera layout.

The review page uses the renderer and panel manager captured from
<https://project-hello-mocha.com/> on 2026-09-27. Its animation controller is
the production controller, with appended playback/scrub hooks. The controller
matches the live site, and three purchased reference clips were also confirmed
identical. The live calibration is X=-90, Y=0, Z=0 for core, arms, and legs.
Reference hashes and comparisons are in `reference/provenance.json`.
Controls now overlay a viewport-sized canvas, as on the website, rather than
subtracting a sidebar from its width. Full visual parity remains unverified.

The only substitution in the captured renderer is the new expression map.
The application controller, quaternion conversion, and root-motion handling
are unchanged. Three.js is local; three-vrm uses the same CDN module as the
site. This preview does not start conversation, trading, or voice services.

`previews/scenario_contact_sheet.jpg` contains Blender pose renders.
Browser screenshots in `previews/face_*.png` show the actual VRM face renderer.
The browser remains the reference for materials, expressions, and spring bones.

## Expressions

The original `happy` expression remains unchanged: a broad smile with closed
eyes for stronger delight. The revised model also contains `smile`, a gentle
open-eye smile built from separate mouth, brow, and eye shapes. Both can be
selected independently. `laugh_closed` remains an explicit choice for laughter.

New choices: `smile`, `warm_smile`, `bright_smile`, `bashful`, `amused`, `playful_wink`,
`tender`, `pout`, `sleepy`, `kiss`, `curious`, and `laugh_closed`.
These are expression bindings made from existing morph targets, not new meshes.
Mesh/texture/rig binary data and all existing speech/blink presets are unchanged.
The original model is backed up locally at
`web/static/animations/Mocha_before_expressions.vrm`; original expression JSON
is also preserved in `expression_recipes.json`.

`character/emotions.yaml` exposes the choices to the LLM. The small new
`web/static/js/emotion-presets.js` mapping is imported by the local renderer
so it can apply the new IDs. Avatars without custom presets fall back to
the closest existing emotion. Face and body remain independently selectable:

```xml
<emotion>tender</emotion>I'm here. Take your time.
<emotion>bashful</emotion>You can't just say that out of nowhere.
```

Expressions currently persist until the next emotion tag. Choose a wink or
kiss for a brief beat and follow with a resting expression. The preview does
not add automatic expression timing to the application.

The prompt now explicitly distinguishes these intensities and contexts.
Routine examples and greeting behaviors use `smile` or `warm_smile`; `happy`
is reserved for stronger delight. Facial choices can stay consistent across
beats when the feeling stays the same, independently of body gestures.

## Catalog and editable assets

The purchased catalog contains **59 functions / 77 variants**. The later
[everyday pass](../everyday/README.md) adds two revised functions, giving 61
functions / 79 variants in the active catalog. All original rows
are preserved. The 15 proposed rows in `animation_functions.additions.csv`
are draft-only and do not enter the conversation prompt. There are 185 local
runtime clips including the 14 preview drafts. `install_pack.py` copies draft
assets for preview but requires `--enable-drafts` to add them to the local
conversation catalog during development.

Open `web/static/animations/Mocha_Scenario_Gestures.blend`, select the Mocha
armature, and select an `SC_` action in the Action Editor. There are 14 editable
actions, baked every three frames at 30 fps. Runtime JSON is sampled at 30 fps.
This Blender scene uses the core glTF importer for posing; it is not a lossless
VRM round-trip file. Do not export it over the runtime VRM.

Repo-visible sources are this pack's original JSON clips, authoring recipes,
metadata, validation reports, and previews. Purchased FBXs, runtime JSONs, the
original-model backup, and the `.blend` are in existing Git-ignored folders.
Those files are present locally but will not appear in a fresh Git clone.
The Blender project is reproducible from the tracked source tools and model.

## Rebuild

From ProjectParrot, with Three.js installed in `scripts/node_modules`:

```powershell
python character/motion_packs/soft_kawaii/tools/build_expressions.py
node character/motion_packs/soft_kawaii/tools/author_scenarios.mjs
node character/motion_packs/soft_kawaii/tools/validate_pack.mjs
node character/motion_packs/soft_kawaii/tools/validate_expressions.mjs
python character/motion_packs/soft_kawaii/tools/install_pack.py
& 'C:\Program Files\Blender Foundation\Blender 5.2\blender.exe' --background --factory-startup --python character/motion_packs/soft_kawaii/tools/build_blender.py
```

For a fresh install, copy the tracked `clips/` through `install_pack.py`.
The existing `scripts/convert_fbx_clips.mjs` rebuilds purchased runtime clips
from the user's FBXs. Blender is required only for the editable scene/renders.
`build_pack.mjs` is a compatibility entry point to the new scenario authoring.

## Validation and limits

`validation.json` checks all 14 clips / 2,696 frames against the actual
production retargeter, plus quaternion validity, timing, loop endpoints, and
an upper bound of 10 degrees per frame on normalized joint changes. All pass;
maximum retarget disagreement is below 0.000003 degrees. Wrist targets at
full pose weight are within 0.7 mm of the authored targets. These numerical
checks do not replace visual judgment of acting or clothing collisions.

`expression_validation.json` verifies that only expression JSON changed in the
VRM; `expression_runtime_validation.json` covers named choices, fallback,
switching, and preservation of speech weights. Poses and representative face
choices were visually checked. Two scenario drafts and two purchased references
now have complete recorded comparisons; the other twelve drafts have not had
this review. This is local asset/runtime review, not a live conversation
evaluation or exhaustive collision test. Numerical PASS does not approve acting.

The existing prompt has conflicting adjacent-gesture instructions in
`character/context.py` (maintain a gesture vs. never repeat it). That behavior
and the player's existing 0.25-second crossfades remain unchanged. No live-site
deployment, commit, or push was performed.

The superseded first pass is recoverable in `.cache/soft_kawaii_v1.zip` and
`.cache/soft_kawaii_v1_clips/`. The pre-edit catalog is backed up at
`.cache/animation_functions.before_soft_kawaii.csv`.
