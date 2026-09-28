# Facial expressions and motion studies

## Release scope

The approved production scope is facial expressions only. Keep the existing purchased body-motion catalog and runtime clips. The procedural EV_/SC_ actions, Blender studies, generated videos, and motion-choice experiments remain research material and are not approved for live use.

The local working catalog has five experimental EV_ entries. Do not deploy that file or run either pack's installation/catalog scripts as part of the facial release. Production has newer code and concurrent work: apply the small expression changes to its current renderer rather than replacing it with the older local renderer.

## Facial behavior

The original `happy` preset remains intact: strong closed-eye delight. `excited` uses it at full strength. `smile` adds mild open-eyed pleasure; `warm_smile` adds affection; `bright_smile` provides a bigger open-eyed grin. Additional choices are `bashful`, `amused`, `playful_wink`, `tender`, `pout`, `sleepy`, `kiss`, `curious`, and `laugh_closed`. Existing neutral, sad, surprised, thinking, playful, and empathetic IDs remain available (20 semantic choices total).

The VRM change adds expression bindings only. Meshes, textures, skeleton, binary buffers, original presets, and speech shapes are preserved. Custom-avatar fallback selects an existing built-in semantic emotion when a custom preset is unavailable. Expression changes clear the previous face without clearing speech viseme weights.

Faces are selected independently of gestures. The LLM receives intensity and context guidance in `character/context.py` and descriptions in `character/emotions.yaml`; ordinary greetings in `character/behaviors.yaml` use smile/warm_smile. Brief wink/kiss/laugh beats need an explicit subsequent resting face because tags persist. Avoid smiling through distress.

Runtime files: `character/Mocha.vrm`, `character/context.py`, `character/emotions.yaml`, `character/behaviors.yaml`, `web/static/js/emotion-presets.js`, and a small change to `web/static/js/vrm-renderer.js`.

Reproducible expression recipes and checks are in `character/motion_packs/soft_kawaii/expression_recipes.json`, `tools/build_expressions.py`, and `tools/validate_expressions.mjs`. Run the validator with Node from the repository root. It checks all mappings, original happy, switching in both directions, custom-avatar fallback, and speech-weight preservation. Building expressions is a model-writing operation; it is unnecessary during deployment of the already validated model.

## Motion research decisions

- The user accepted the renderer calibration; the procedural acting was the remaining issue. Do not revisit coordinates based solely on those acting critiques.
- Of the five procedural everyday gestures, the user considered “I'm listening” acceptable. “I'm here” and “my mistake” had unclear intent. Numerical validity did not establish natural or appealing movement.
- All future performance references must be standing. No sitting.
- Define the situation and emotional intention first. Author readable action with anticipation, a meaningful hold, and a gentle return to idle.
- Approved personality: chill, calm, caring; quiet enthusiasm through purposeful movement, restrained smiles, relaxed shoulders, and natural pauses. Avoid bubbly bouncing or exaggerated surprise.
- Approved visual direction: a cel-shaded 3D anime adult woman with painted textures and subtle volumetric lighting. This is now the default in the reusable prompt. The sage top, navy trousers, and white sneakers are reference styling, not a change to Mocha's model.
- The two-handed calming gesture and three later clips were accepted as useful for testing. That is not approval to register new live actions.

## Prompt and generated references

Use [the saved performance prompt](../character/motion_packs/everyday/PERFORMANCE_PROMPT_TEMPLATE.md). Exact generation prompts, IDs, settings, and available result URLs are retained in the five `higgsfield_*.json` files beside it. The latest batch contains three results in index order: offered hand, quiet applause, gentle pause. These remote links are provider-hosted references, not guaranteed permanent archives.

| Study | Result / decision |
| --- | --- |
| First human standing reference | Too static; rejected as useful acting |
| Human greeting | Movement acceptable, personality too energetic |
| Human “take a breath with me” | Requested revision to two hands and anime rendering |
| Two-handed 3D anime breath | Visual style approved |
| Offered hand / quiet applause / gentle pause | Accepted for testing; no motion capture or live registration |

Seven videos were generated at an estimated 60 credits each (420 total). Later previews reused results and did not regenerate them. Completion was confirmed by the provider. Full playback quality was not independently verified for the later clips; do not describe them as production-ready motion.

Higgsfield video output is MP4, not an editable mesh, VRM, skeleton, or FBX. Its separate mesh-generation tools can generate textured models and offer preset animations, but they were not used for these custom gestures. A visual texture alone does not create skeletal movement. Anime styling can change perceived acting; it does not establish accurate motion recovery.

Proposed next pipeline: evaluate the chosen video in motion capture, export skeletal motion, clean up/retarget in Blender, then validate against Mocha's actual runtime and original purchased references. Anime-footage capture support and quality still need validation. DeepMotion setup was pending; no generated reference has been converted or installed. Facial animation remains a separate runtime channel.

## Local artifacts and historical notes

[Everyday pack notes](../character/motion_packs/everyday/README.md) describe the five procedural studies, Blender scene, and local review gallery. [Earlier soft/kawaii notes](../character/motion_packs/soft_kawaii/README.md) describe superseded experiments. Their catalog counts and earlier “not deployed” statements are historical; this document controls current release scope.

Local preview: `http://127.0.0.1:8766/character/motion_packs/everyday/review.html`. Purchased FBX assets and generated runtime clips are ignored by Git. Large Blender files and preview movies remain local research artifacts. Do not bulk-add the motion-pack directory when saving documentation; select documentation, prompt metadata, and expression source/check files explicitly.

## Production operations

The running service is `project-mocha` on the configured `HomePCBlackwell` SSH host, working directory `/home/tianyizhang/ProjectMocha`. The checked-in older unit file's opus-trading path is stale. Inspect the live service and dirty worktree before each release. Preserve concurrent changes, take a file-level backup, guard against changes since inspection, then apply only the six runtime files listed above. Restart the bridge to reload its Python prompt code; static renderer/model resources are served from disk.

Verify the public JavaScript, `/api/default-model` bytes, bridge health, and unchanged production animation catalog. Reload the browser to load the model's new presets. A live end-to-end conversation remains a separate subjective check of the LLM's expression choices.

### Completed facial-only release

Deployed on 2026-09-27 (server backup timestamp 23:41:10), against server Git revision `6ca72302b7a35fb664af02ae0616c5e119e49628` plus its existing uncommitted work. No Git reset, checkout, pull, or bulk replacement was performed on production. The server renderer already imported `VISEME_NAMES` and old emotion mappings from `avatar-cues.js`; the release retained that viseme import, imported the new expression mapping/resolver separately, and changed only emotion resolution in `applyEmotion`.

Backup: `/home/tianyizhang/ProjectMocha/.cache/face-release-20260927-234110/before.tar.gz`. The adjacent `release.json` records deployed file hashes. Restore the six face files as a set for rollback, accounting for any edits made since deployment; the newly added `emotion-presets.js` had no prior version. Reload bridge prompt code after rollback.

Validation completed:

- Production VRM matched the original local model before changes. Revised model changed expression JSON only; original preset expressions and binary mesh/rig/texture buffers remained identical.
- Both local and production-adapted renderer passed all 20 emotion mappings, 12 custom presets, original happy preservation, happy/smile distinction, switching/clearing, avatar fallback, and speech-viseme preservation checks.
- Production Python prompt compiled; YAML parsed with 20 emotions.
- Guarded deployment verified files had not changed since backup. The production animation catalog remained byte-identical; no EV_/SC_ payloads or generated video assets were deployed.
- Only the bridge process was gracefully restarted. Its health endpoint returned HTTP 200. Web and TTS were not stopped.
- Public `/static/js/emotion-presets.js`, `/static/js/vrm-renderer.js`, and `/api/default-model` returned HTTP 200 and exactly matched the staged release bytes.

Public model SHA-256: `d0c59b8b07217de5e5ea068df6d43fd0d067f28540dc56411af92466b2a92fc8`.
