/**
 * convert_fbx_clips.mjs — Convert FBX animations to JSON delta clips.
 *
 * Exports per-bone DELTA quaternions: inv(restLocal) * animLocal
 * This strips the skeleton's rest orientation, leaving only the animation
 * change from rest. Deltas are applied to VRM normalized bones (identity rest).
 *
 * Usage:
 *   node scripts/convert_fbx_clips.mjs web/static/animations/ -o web/static/clips/
 */

import * as THREE from 'three';
import { FBXLoader } from 'three/addons/loaders/FBXLoader.js';
import fs from 'fs';
import path from 'path';
import { FileLoader } from 'three';

// ── Polyfill for Node.js ─────────────────────────────────────────────────────

FileLoader.prototype.load = function(url, onLoad, onProgress, onError) {
    try {
        const data = fs.readFileSync(url);
        const ab = data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength);
        if (this.responseType === 'arraybuffer') {
            if (onLoad) onLoad(ab);
        } else {
            if (onLoad) onLoad(data.toString('utf-8'));
        }
    } catch (e) {
        if (onError) onError(e);
    }
    return {};
};

if (typeof Blob === 'undefined') globalThis.Blob = class Blob {};

// ── Kawaii FBX → VRM bone name mapping ───────────────────────────────────────

const BONE_MAP = {
    // Core
    'Hips': 'Hips', 'Spine': 'Spine', 'Chest': 'Chest',
    'Upper_Chest': 'UpperChest', 'Neck': 'Neck', 'Head': 'Head',
    // Arms
    'Shoulder_L': 'LeftShoulder', 'Shoulder_R': 'RightShoulder',
    'Upper_Arm_L': 'LeftUpperArm', 'Upper_Arm_R': 'RightUpperArm',
    'Lower_Arm_L': 'LeftLowerArm', 'Lower_Arm_R': 'RightLowerArm',
    'Hand_L': 'LeftHand', 'Hand_R': 'RightHand',
    // Legs
    'Upper_Leg_L': 'LeftUpperLeg', 'Upper_Leg_R': 'RightUpperLeg',
    'Lower_Leg_L': 'LeftLowerLeg', 'Lower_Leg_R': 'RightLowerLeg',
    'Foot_L': 'LeftFoot', 'Foot_R': 'RightFoot',
    'Toes_L': 'LeftToes', 'Toes_R': 'RightToes',
    // Left fingers
    'Thumb_Proximal_L': 'LeftThumbMetacarpal',
    'Thumb_Intermediate_L': 'LeftThumbProximal',
    'Thumb_Distal_L': 'LeftThumbDistal',
    'Index_Proximal_L': 'LeftIndexProximal',
    'Index_Intermediate_L': 'LeftIndexIntermediate',
    'Index_Distal_L': 'LeftIndexDistal',
    'Middle_Proximal_L': 'LeftMiddleProximal',
    'Middle_Intermediate_L': 'LeftMiddleIntermediate',
    'Middle_Distal_L': 'LeftMiddleDistal',
    'Ring_Proximal_L': 'LeftRingProximal',
    'Ring_Intermediate_L': 'LeftRingIntermediate',
    'Ring_Distal_L': 'LeftRingDistal',
    'Little_Proximal_L': 'LeftLittleProximal',
    'Little_Intermediate_L': 'LeftLittleIntermediate',
    'Little_Distal_L': 'LeftLittleDistal',
    // Right fingers
    'Thumb_Proximal_R': 'RightThumbMetacarpal',
    'Thumb_Intermediate_R': 'RightThumbProximal',
    'Thumb_Distal_R': 'RightThumbDistal',
    'Index_Proximal_R': 'RightIndexProximal',
    'Index_Intermediate_R': 'RightIndexIntermediate',
    'Index_Distal_R': 'RightIndexDistal',
    'Middle_Proximal_R': 'RightMiddleProximal',
    'Middle_Intermediate_R': 'RightMiddleIntermediate',
    'Middle_Distal_R': 'RightMiddleDistal',
    'Ring_Proximal_R': 'RightRingProximal',
    'Ring_Intermediate_R': 'RightRingIntermediate',
    'Ring_Distal_R': 'RightRingDistal',
    'Little_Proximal_R': 'RightLittleProximal',
    'Little_Intermediate_R': 'RightLittleIntermediate',
    'Little_Distal_R': 'RightLittleDistal',
};

const SAMPLE_FPS = 30;

// ── FBX loading + delta extraction ───────────────────────────────────────────

function loadFBX(filePath) {
    return new Promise((resolve, reject) => {
        new FBXLoader().load(filePath, resolve, undefined, reject);
    });
}

function findBone(root, name) {
    let found = null;
    root.traverse(c => { if (c.isBone && c.name === name) found = c; });
    return found;
}

function round6(n) { return Math.round(n * 1000000) / 1000000; }

function extractClip(fbxGroup, animClip) {
    const duration = animClip.duration;
    if (duration <= 0) return null;

    const frameCount = Math.max(1, Math.round(duration * SAMPLE_FPS));
    const dt = duration / frameCount;

    // Find bones
    const boneRefs = {};
    for (const [fbxName, vrmName] of Object.entries(BONE_MAP)) {
        const bone = findBone(fbxGroup, fbxName);
        if (bone) boneRefs[vrmName] = bone;
    }
    if (Object.keys(boneRefs).length === 0) return null;

    // Capture Hips rest position (before animation) for position delta
    const hipsBone = boneRefs['Hips'];
    const hipsRestPos = hipsBone ? hipsBone.position.clone() : null;

    // Play animation and sample RAW LOCAL quaternions + Hips position per frame
    const mixer = new THREE.AnimationMixer(fbxGroup);
    mixer.clipAction(animClip).play();

    const frames = [];

    for (let f = 0; f < frameCount; f++) {
        mixer.setTime(f * dt);
        fbxGroup.updateMatrixWorld(true);

        const frame = {};
        for (const [vrmName, bone] of Object.entries(boneRefs)) {
            const q = bone.quaternion;
            frame[vrmName] = [round6(q.x), round6(q.y), round6(q.z), round6(q.w)];
        }

        // Hips position delta (cm, relative to rest position)
        if (hipsBone && hipsRestPos) {
            const p = hipsBone.position;
            frame._hipsPos = [
                round6(p.x - hipsRestPos.x),
                round6(p.y - hipsRestPos.y),
                round6(p.z - hipsRestPos.z),
            ];
        }

        frames.push(frame);
    }

    mixer.stopAllAction();

    return {
        name: animClip.name || 'default',
        duration: Math.round(duration * 10000) / 10000,
        fps: SAMPLE_FPS,
        frameCount,
        frames,
    };
}

// ── Main ─────────────────────────────────────────────────────────────────────

async function main() {
    const args = process.argv.slice(2);
    let inputPath = args.find(a => !a.startsWith('-'));
    let outputDir = 'web/static/clips';

    const oIdx = args.indexOf('-o');
    if (oIdx >= 0 && args[oIdx + 1]) outputDir = args[oIdx + 1];

    if (!inputPath) {
        console.log('Usage: node convert_fbx_clips.mjs <dir/> [-o output/]');
        process.exit(1);
    }

    fs.mkdirSync(outputDir, { recursive: true });

    const stat = fs.statSync(inputPath);
    const files = stat.isDirectory()
        ? fs.readdirSync(inputPath).filter(f => f.toLowerCase().endsWith('.fbx')).sort().map(f => path.join(inputPath, f))
        : [inputPath];

    console.log(`Processing ${files.length} FBX files (delta mode)...`);
    const durations = {};
    let exported = 0;

    for (const file of files) {
        try {
            const fbxGroup = await loadFBX(file);
            const basename = path.basename(file, path.extname(file)).replace(/^@/, '');
            const clips = fbxGroup.animations || [];
            if (clips.length === 0) { console.log(`  ${basename}: no animations`); continue; }

            const result = extractClip(fbxGroup, clips[0]);
            if (!result) { console.log(`  ${basename}: no bones`); continue; }

            result.name = basename;
            const outPath = path.join(outputDir, `${basename}.json`);
            fs.writeFileSync(outPath, JSON.stringify(result));
            const sizeKB = (fs.statSync(outPath).size / 1024).toFixed(0);
            console.log(`  ${basename}.json — ${result.frameCount} frames, ${result.duration}s, ${sizeKB}KB`);

            durations[basename] = result.duration;
            exported++;
        } catch (e) {
            console.error(`  ERROR ${path.basename(file)}: ${e.message}`);
        }
    }

    // Write duration manifest
    const manifestPath = path.join(outputDir, '_clip_durations.json');
    fs.writeFileSync(manifestPath, JSON.stringify(durations, null, 2));
    console.log(`\nDone. ${exported}/${files.length} clips → ${outputDir}/`);
}

main().catch(e => { console.error(e); process.exit(1); });
