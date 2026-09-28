> Historical local research notes. Motion scripts, clips, Blender scenes, and preview movies referenced below remain local experiments and are not included in the facial-only release. See [current release scope](../../../docs/character-expression-and-motion-work.md).

# Five everyday moments

This pass deliberately covers five common scenarios not explicitly represented
in the purchased motion library. It supersedes the corresponding SC_ drafts.

| Function | Scenario | Acting | Face | Duration |
| --- | --- | --- | --- | --- |
| `here_for_you` | Someone is tired or discouraged and wants company | Listen, soften knees, hand over heart, small open palm, nod, settle | `tender` | 6.8 s |
| `sleepy_goodnight` | Ending a warm conversation at bedtime | Exhale, gather hands beside cheek, head follows, sleepy hold, slow release | `sleepy` | 8.4 s |
| `listening_acknowledgment` | Listening while someone tells a story | Lean closer, head tilt, two uneven nods, attentive hold | `curious` | 6.6 s |
| `gentle_apology` | A harmless misunderstanding or missed detail | Downward glance, hand to chest, modest bow, look back up | `bashful` | 6.8 s |
| `stay_a_little` | Inviting someone to keep chatting if they have time | Small open palm, inward invitation, head tilt, release | `warm_smile` | 7.6 s |

Waves, shy reactions, hugs, kisses, teasing, and cheers already exist in the
purchased files. Hugs and kisses were found among the FBXs even though they are
not currently exposed in the original conversation catalog. No purchased clip
was sampled, slowed, or rewritten to create these actions.

## Review and assets

Run the existing local preview server and open:
`http://127.0.0.1:8766/character/motion_packs/everyday/review.html`.

`previews/*.webm` are complete real-time takes in Mocha's production controller
and captured website renderer, with the actual facial presets. Each was recorded
from beginning to end and its timed frame sequence inspected. The HTML player
offers normal-speed replay. Captures are finalized with FFmpeg seek indexes, with
MP4 alternatives; replay resets the decoder and displays elapsed time. Videos
are cropped horizontally around the character for easier inspection.
Full-body inspection uses camera (0, .9, 5), target
(0, .9, 0), FOV 25 degrees; Website framing is available in interactive rehearsal.

`Mocha_Everyday.blend` contains the VRM-add-on import and five editable actions.
It also preserves the user's initial default scene as a separate scene. Select
the Mocha armature and choose the EV_ action in the Action Editor. Earlier local
passes have names ending in `previous pass`. Face values are a preview setting;
the app selects face independently of body. Do not export this working scene
over the production VRM.

`clips/EV_*.json` are deployable motion payloads for the existing Mocha retargeter.
`normalized.json` holds the matching normalized poses used in Blender.
`author.mjs` contains the choreography recipes. `kinematics.mjs` shares the
original retargeting encoder and wrist IK math with the first draft; elbow poles
were revised, and this pass adds planted-foot leg IK and pelvis/spine motion.
Authoring is scripted keyframing with visual iteration in Blender, not mocap.

## Changes made during review

- Replaced entry/exit rotation blending with wrist paths that arc forward of
  the body, then resolve the elbow and upper arm at every frame.
- Added small clavicle elevation and forward shoulder movement during reaches.
- Moved axial palm turning into the forearm and capped residual wrist rotation
  at 38 degrees. The former goodnight pose used about 139 degrees of total local
  wrist rotation; the new solution stays within the cap. This is an animation
  constraint, not a complete anatomical model.
- Added a previous reassurance recording for direct comparison. All four
  raised-arm takes were rerecorded; the listening take has no raised-arm action.
- Removed the unnecessary second raised arm from reassurance.
- Lowered elbow targets to reduce the flared-arm silhouette.
- Added small asymmetrical pelvis shifts, spine motion, knee flexion, and nods.
- Narrowed the goodnight hand spacing and replaced an abrupt turning wrist
  transition with a longer joint-space release.
- Kept foot targets fixed during each clip. Idle crossfades use the application's
  existing blending and have not been evaluated in a live conversation.

Numerical checks pass for 1,091 frames, including the actual production retargeter,
unit quaternions, timing, and a maximum normalized joint step below 10 degrees.
Analytical ankle drift is below 1e-12 m. This is not a mesh collision guarantee
or a claim of artist approval. Hand, sleeve, and hair intersections should still
be judged in the playable takes.

## Rebuild

From ProjectParrot:

```powershell
node character/motion_packs/everyday/author.mjs
node character/motion_packs/everyday/validate.mjs
node character/motion_packs/everyday/check_arms.mjs
```

`load_blender.py` loads the normalized actions into an already imported Mocha
armature using the Blender MCP. It preserves earlier action passes. The script
currently contains this workstation's project path; change ROOT when relocating.

Only these five functions are added to the active catalog, with one-shot playback
and one repeat each. The other 14 SC_ prototype files remain outside the catalog.

`prepare_previews.py` finalizes browser captures and rebuilds the gallery. It
uses Pillow and imageio-ffmpeg from the local `.cache/motion-qa-deps` directory.
`update_catalog.py` registers the pack and checks the actual LLM gesture block,
including preservation of all 77 purchased catalog rows.
