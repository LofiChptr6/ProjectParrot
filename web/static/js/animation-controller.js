/**
 * animation-controller.js — FBX→VRM retargeting via world-space chain math.
 *
 * For each bone per frame:
 *   1. Recover FBX animated local: animLocal = restLocal * delta
 *   2. Chain-multiply to get FBX animated world quaternion
 *   3. Chain-multiply rest locals to get FBX rest world quaternion
 *   4. Compute world result: boneResult = inv(restWorld) * animWorld
 *      (coordinate frame cancels automatically!)
 *   5. Convert to VRM normalized local: inv(parentResult) * boneResult
 *
 * No calibration, no conjugation, no hidden skeleton. Pure math.
 *
 * State machine:
 *   IDLE -> STARTING -> LOOPING -> ENDING -> IDLE
 *   IDLE -> PLAYING_ONCE -> IDLE
 */

import * as THREE from 'three';

// ============================================================================
//  Constants
// ============================================================================

const CLIP_BASE_URL = '/static/clips/';
const CROSSFADE_SEC = 0.25;
const FIDGET_MIN = 8;
const FIDGET_MAX = 15;
const IDLE_FUNCTION = 'idle_breathe';
const DEG = Math.PI / 180;

// ============================================================================
//  Per-section axis calibration — conjugation R * result * inv(R)
//  Corrects the rotation axis direction after chain math.
//  Spine/Hips are already correct. Arms and legs need axis remapping.
// ============================================================================

const SECTION_MAP = {
    Hips: 'core', Spine: 'core', Chest: 'core', UpperChest: 'core',
    Neck: 'core', Head: 'core',
    LeftShoulder: 'arm', LeftUpperArm: 'arm', LeftLowerArm: 'arm', LeftHand: 'arm',
    RightShoulder: 'arm', RightUpperArm: 'arm', RightLowerArm: 'arm', RightHand: 'arm',
    LeftUpperLeg: 'leg', LeftLowerLeg: 'leg', LeftFoot: 'leg', LeftToes: 'leg',
    RightUpperLeg: 'leg', RightLowerLeg: 'leg', RightFoot: 'leg', RightToes: 'leg',
    // Fingers follow their arm section
    LeftThumbMetacarpal:'arm', LeftThumbProximal:'arm', LeftThumbDistal:'arm',
    LeftIndexProximal:'arm', LeftIndexIntermediate:'arm', LeftIndexDistal:'arm',
    LeftMiddleProximal:'arm', LeftMiddleIntermediate:'arm', LeftMiddleDistal:'arm',
    LeftRingProximal:'arm', LeftRingIntermediate:'arm', LeftRingDistal:'arm',
    LeftLittleProximal:'arm', LeftLittleIntermediate:'arm', LeftLittleDistal:'arm',
    RightThumbMetacarpal:'arm', RightThumbProximal:'arm', RightThumbDistal:'arm',
    RightIndexProximal:'arm', RightIndexIntermediate:'arm', RightIndexDistal:'arm',
    RightMiddleProximal:'arm', RightMiddleIntermediate:'arm', RightMiddleDistal:'arm',
    RightRingProximal:'arm', RightRingIntermediate:'arm', RightRingDistal:'arm',
    RightLittleProximal:'arm', RightLittleIntermediate:'arm', RightLittleDistal:'arm',
};

const sectionCal = {
    core: { x: -90, y: 0, z: 0 },
    arm:  { x: -90, y: 0, z: 0 },
    leg:  { x: -90, y: 0, z: 0 },
};

window._sectionCal = sectionCal;

const _corrQ = new THREE.Quaternion();
const _corrInv = new THREE.Quaternion();
const _corrDelta = new THREE.Quaternion();

function applySectionCorrection(qArr, boneName) {
    const sec = SECTION_MAP[boneName];
    if (!sec) return qArr;
    const c = sectionCal[sec];
    if (!c || (c.x === 0 && c.y === 0 && c.z === 0)) return qArr;

    _corrQ.setFromEuler(new THREE.Euler(c.x * DEG, c.y * DEG, c.z * DEG));
    _corrDelta.set(qArr[0], qArr[1], qArr[2], qArr[3]);
    _corrInv.copy(_corrQ).invert();
    _corrDelta.premultiply(_corrQ).multiply(_corrInv);
    return [_corrDelta.x, _corrDelta.y, _corrDelta.z, _corrDelta.w];
}

// ============================================================================
//  FBX Kawaii skeleton: rest local quaternions + hierarchy
//  Captured from KA_Idle01_breathing.fbx bind pose.
//  These are CONSTANT — same for every clip from this skeleton.
// ============================================================================

const FBX_REST = {
    Hips:[0.7067575,0.0222189,0.7067572,0.022235],
    Spine:[0.0000158,-4e-7,0.1137302,0.9935117],
    Chest:[-0.0000117,6e-7,-0.1128586,0.9936111],
    UpperChest:[0.0000066,0,-0.0859249,0.9963016],
    Neck:[0.0000031,0.0000013,0.2245775,0.9744562],
    Head:[0.0000151,-0.0000026,-0.0947215,0.9955038],
    LeftShoulder:[0.6579976,-0.0122255,0.7523569,-0.0291338],
    RightShoulder:[-0.657997,0.0122221,0.7523576,-0.0291309],
    LeftUpperArm:[0.0770229,-0.0456933,-0.0089203,0.9959418],
    RightUpperArm:[-0.0770223,0.0456923,-0.0089156,0.9959419],
    LeftLowerArm:[-1e-7,1e-7,-0.058363,0.9982954],
    RightLowerArm:[0,1e-7,-0.058363,0.9982954],
    LeftHand:[-0.5919213,0.054261,0.0794718,0.8002307],
    RightHand:[0.5919214,-0.0542605,0.0794717,0.8002307],
    LeftUpperLeg:[-0.0401789,0.9962391,0.0729606,-0.0238743],
    RightUpperLeg:[-0.0402386,0.9962391,-0.0729297,0.0238705],
    LeftLowerLeg:[-0.0000015,-1e-7,-0.0095765,0.9999541],
    RightLowerLeg:[-0.0000023,-4e-7,-0.009437,0.9999555],
    LeftFoot:[-0.0000296,-0.0221616,0.001014,0.9997539],
    RightFoot:[0.0000136,0.0221632,0.0009357,0.9997539],
    LeftToes:[0.0000108,-0.0000104,0.7071068,0.7071068],
    RightToes:[0.0000131,-0.0000127,0.7071068,0.7071068],
    LeftThumbMetacarpal:[0.5791413,0.322024,-0.0583363,0.7466544],
    LeftThumbProximal:[0.0002346,-0.0551599,0.174874,0.9830445],
    LeftThumbDistal:[-0.0002777,0.0000743,0.0733976,0.9973027],
    LeftIndexProximal:[0.0693729,-0.0310864,0.114883,0.990466],
    LeftIndexIntermediate:[-0.0008872,0.0001329,0.1050702,0.9944644],
    LeftIndexDistal:[0.0003468,0.0004845,-0.0027402,0.9999961],
    LeftMiddleProximal:[-0.0311577,-0.058645,0.1446255,0.9872555],
    LeftMiddleIntermediate:[0.0010545,0.0029757,0.166636,0.9860134],
    LeftMiddleDistal:[-0.000101,-0.0000979,0.0232776,0.999729],
    LeftRingProximal:[-0.11459,-0.0942866,0.1171987,0.9819591],
    LeftRingIntermediate:[-0.00108,0.0043469,0.2290852,0.9733961],
    LeftRingDistal:[-0.0019581,-0.0021218,0.0403652,0.9991808],
    LeftLittleProximal:[-0.2215838,-0.1681941,0.13138,0.9514992],
    LeftLittleIntermediate:[-0.0005652,-0.0017242,0.1762064,0.9843516],
    LeftLittleDistal:[0.0004249,-0.0002733,0.0284017,0.9995965],
    RightThumbMetacarpal:[-0.5791411,-0.3220242,-0.058336,0.7466545],
    RightThumbProximal:[-0.0002343,0.0551599,0.1748748,0.9830443],
    RightThumbDistal:[0.0002755,-0.0000738,0.0733939,0.997303],
    RightIndexProximal:[-0.0693712,0.0310918,0.1148857,0.9904656],
    RightIndexIntermediate:[0.0008808,-0.0001441,0.1050641,0.9944651],
    RightIndexDistal:[-0.00034,-0.0004752,-0.0027345,0.9999961],
    RightMiddleProximal:[0.0311561,0.0586406,0.1446246,0.987256],
    RightMiddleIntermediate:[-0.0010519,-0.0029717,0.1666374,0.9860132],
    RightMiddleDistal:[0.0001009,0.0000977,0.0232777,0.999729],
    RightRingProximal:[0.1145901,0.0942881,0.1171993,0.9819588],
    RightRingIntermediate:[0.0010808,-0.0043455,0.2290854,0.973396],
    RightRingDistal:[0.0019571,0.0021208,0.0403649,0.9991808],
    RightLittleProximal:[0.2215839,0.1681962,0.1313809,0.9514986],
    RightLittleIntermediate:[0.0005665,0.0017259,0.1762061,0.9843516],
    RightLittleDistal:[-0.0004227,0.0002753,0.0284016,0.9995965],
};

/** FBX humanoid parent chain */
const PARENT = {
    Hips:null, Spine:'Hips', Chest:'Spine', UpperChest:'Chest',
    Neck:'UpperChest', Head:'Neck',
    LeftShoulder:'UpperChest', LeftUpperArm:'LeftShoulder',
    LeftLowerArm:'LeftUpperArm', LeftHand:'LeftLowerArm',
    RightShoulder:'UpperChest', RightUpperArm:'RightShoulder',
    RightLowerArm:'RightUpperArm', RightHand:'RightLowerArm',
    LeftUpperLeg:'Hips', LeftLowerLeg:'LeftUpperLeg',
    LeftFoot:'LeftLowerLeg', LeftToes:'LeftFoot',
    RightUpperLeg:'Hips', RightLowerLeg:'RightUpperLeg',
    RightFoot:'RightLowerLeg', RightToes:'RightFoot',
    LeftThumbMetacarpal:'LeftHand', LeftThumbProximal:'LeftThumbMetacarpal', LeftThumbDistal:'LeftThumbProximal',
    LeftIndexProximal:'LeftHand', LeftIndexIntermediate:'LeftIndexProximal', LeftIndexDistal:'LeftIndexIntermediate',
    LeftMiddleProximal:'LeftHand', LeftMiddleIntermediate:'LeftMiddleProximal', LeftMiddleDistal:'LeftMiddleIntermediate',
    LeftRingProximal:'LeftHand', LeftRingIntermediate:'LeftRingProximal', LeftRingDistal:'LeftRingIntermediate',
    LeftLittleProximal:'LeftHand', LeftLittleIntermediate:'LeftLittleProximal', LeftLittleDistal:'LeftLittleIntermediate',
    RightThumbMetacarpal:'RightHand', RightThumbProximal:'RightThumbMetacarpal', RightThumbDistal:'RightThumbProximal',
    RightIndexProximal:'RightHand', RightIndexIntermediate:'RightIndexProximal', RightIndexDistal:'RightIndexIntermediate',
    RightMiddleProximal:'RightHand', RightMiddleIntermediate:'RightMiddleProximal', RightMiddleDistal:'RightMiddleIntermediate',
    RightRingProximal:'RightHand', RightRingIntermediate:'RightRingProximal', RightRingDistal:'RightRingIntermediate',
    RightLittleProximal:'RightHand', RightLittleIntermediate:'RightLittleProximal', RightLittleDistal:'RightLittleIntermediate',
};

/** Processing order: parent before child */
const BONE_ORDER = [
    'Hips','Spine','Chest','UpperChest','Neck','Head',
    'LeftShoulder','LeftUpperArm','LeftLowerArm','LeftHand',
    'RightShoulder','RightUpperArm','RightLowerArm','RightHand',
    'LeftUpperLeg','LeftLowerLeg','LeftFoot','LeftToes',
    'RightUpperLeg','RightLowerLeg','RightFoot','RightToes',
    'LeftThumbMetacarpal','LeftThumbProximal','LeftThumbDistal',
    'LeftIndexProximal','LeftIndexIntermediate','LeftIndexDistal',
    'LeftMiddleProximal','LeftMiddleIntermediate','LeftMiddleDistal',
    'LeftRingProximal','LeftRingIntermediate','LeftRingDistal',
    'LeftLittleProximal','LeftLittleIntermediate','LeftLittleDistal',
    'RightThumbMetacarpal','RightThumbProximal','RightThumbDistal',
    'RightIndexProximal','RightIndexIntermediate','RightIndexDistal',
    'RightMiddleProximal','RightMiddleIntermediate','RightMiddleDistal',
    'RightRingProximal','RightRingIntermediate','RightRingDistal',
    'RightLittleProximal','RightLittleIntermediate','RightLittleDistal',
];

// ============================================================================
//  Pre-compute FBX rest world quaternions (chain multiply once at startup)
// ============================================================================

const _q = new THREE.Quaternion();
const FBX_REST_WORLD = {};

for (const bone of BONE_ORDER) {
    const r = FBX_REST[bone];
    if (!r) continue;
    const restLocal = new THREE.Quaternion(r[0], r[1], r[2], r[3]);
    const parent = PARENT[bone];
    if (parent && FBX_REST_WORLD[parent]) {
        FBX_REST_WORLD[bone] = FBX_REST_WORLD[parent].clone().multiply(restLocal);
    } else {
        FBX_REST_WORLD[bone] = restLocal.clone();
    }
}

// ============================================================================
//  State
// ============================================================================

const State = Object.freeze({
    IDLE: 'IDLE', STARTING: 'STARTING', LOOPING: 'LOOPING',
    ENDING: 'ENDING', PLAYING_ONCE: 'PLAYING_ONCE',
});

let state = State.IDLE;
let ready = false;
let functionTable = [];
let functionMap = new Map();
let idleFunctions = [];

let activeFunction = null;
let activeVariant = null;
let pendingEnd = false;
let loopsPlayed = 0;
let fidgetTimer = randomFidget();

let currentClip = null;
let clipTime = 0;
let clipLoop = false;

let crossfading = false;
let crossfadeTime = 0;
let snapshotPose = null;

const clipCache = new Map();
const clipLoading = new Map();

// Temp quaternions
const _qa = new THREE.Quaternion();
const _qb = new THREE.Quaternion();
const _restW = new THREE.Quaternion();
const _animW = new THREE.Quaternion();
const _result = new THREE.Quaternion();
const _parentResult = new THREE.Quaternion();

// ============================================================================
//  Initialization
// ============================================================================

async function init() {
    try {
        const resp = await fetch('/api/animation-functions');
        const data = await resp.json();
        functionTable = data.functions || [];

        for (const entry of functionTable) {
            const name = entry.function;
            if (!functionMap.has(name)) functionMap.set(name, []);
            functionMap.get(name).push(entry);
            if (entry.category === 'idle' && name !== IDLE_FUNCTION) {
                idleFunctions.push(entry);
            }
        }
        const seen = new Set();
        idleFunctions = idleFunctions.filter(e => {
            if (seen.has(e.function)) return false;
            seen.add(e.function);
            return true;
        });

        ready = true;
        console.log(`[AnimController] Ready: ${functionTable.length} entries, ${BONE_ORDER.length} bones`);
        goIdle();
    } catch (err) {
        console.error('[AnimController] Init failed:', err);
    }
}

init();

// ============================================================================
//  Clip loading
// ============================================================================

async function loadClip(clipName) {
    if (clipCache.has(clipName)) return clipCache.get(clipName);
    if (clipLoading.has(clipName)) return clipLoading.get(clipName);

    const promise = fetch(`${CLIP_BASE_URL}${clipName}.json?v=${Date.now()}`)
        .then(r => r.ok ? r.json() : null)
        .then(data => {
            if (data) clipCache.set(clipName, data);
            clipLoading.delete(clipName);
            return data;
        })
        .catch(err => {
            console.error(`[AnimController] Load failed: ${clipName}`, err);
            clipLoading.delete(clipName);
            return null;
        });

    clipLoading.set(clipName, promise);
    return promise;
}

// ============================================================================
//  Core retargeting: delta frame → VRM normalized bone quaternions
//
//  For each bone:
//    animLocal = restLocal * delta
//    animWorld = parentAnimWorld * animLocal
//    restWorld = pre-computed (FBX_REST_WORLD)
//    boneResult = inv(restWorld) * animWorld  ← frame cancels!
//    normalizedLocal = inv(parentResult) * boneResult
// ============================================================================

function retargetFrame(frame) {
    const animWorlds = {};
    const results = {};
    const output = {};

    for (const bone of BONE_ORDER) {
        const rawLocal = frame[bone];
        if (!rawLocal) continue;

        // 1. Raw FBX animated local (directly from clip — includes rest pose)
        _qa.set(rawLocal[0], rawLocal[1], rawLocal[2], rawLocal[3]);

        // 2. Chain to get animated world
        const parent = PARENT[bone];
        if (parent && animWorlds[parent]) {
            animWorlds[bone] = animWorlds[parent].clone().multiply(_qa);
        } else {
            animWorlds[bone] = _qa.clone();
        }

        // 3. boneResult = animWorld * inv(restWorld)  ← world-frame delta
        _restW.copy(FBX_REST_WORLD[bone]).invert();
        results[bone] = animWorlds[bone].clone().multiply(_restW);

        // 4. normalizedLocal = inv(parentResult) * boneResult
        if (parent && results[parent]) {
            _parentResult.copy(results[parent]).invert();
            _result.copy(_parentResult).multiply(results[bone]);
        } else {
            _result.copy(results[bone]);
        }

        const corrected = applySectionCorrection(
            [_result.x, _result.y, _result.z, _result.w], bone);
        output[bone] = corrected;
    }

    // Pass through Hips position if present
    if (frame._hipsPos) output._hipsPos = frame._hipsPos;

    // Debug: detailed trace for Hips (once)
    if (!retargetFrame._logged && output.Hips) {
        retargetFrame._logged = true;
        const hRaw = frame['Hips'];
        const hRest = FBX_REST['Hips'];
        const hRestW = FBX_REST_WORLD['Hips'];
        console.log('[Retarget] === HIPS TRACE ===');
        console.log('  clip raw local:', hRaw?.map(v=>v.toFixed(4)));
        console.log('  FBX_REST local:', hRest?.map(v=>v.toFixed(4)));
        console.log('  FBX_REST_WORLD:', [hRestW.x.toFixed(4), hRestW.y.toFixed(4), hRestW.z.toFixed(4), hRestW.w.toFixed(4)]);
        console.log('  animWorld:', [animWorlds['Hips'].x.toFixed(4), animWorlds['Hips'].y.toFixed(4), animWorlds['Hips'].z.toFixed(4), animWorlds['Hips'].w.toFixed(4)]);
        console.log('  result:', [results['Hips'].x.toFixed(4), results['Hips'].y.toFixed(4), results['Hips'].z.toFixed(4), results['Hips'].w.toFixed(4)]);
        console.log('  output:', output.Hips.map(v=>v.toFixed(4)));
        const h = output.Hips;
        const angle = 2 * Math.acos(Math.min(1, Math.abs(h[3]))) * 180 / Math.PI;
        console.log('  angle:', angle.toFixed(1) + '°');
    }

    return output;
}

// ============================================================================
//  Frame sampling with slerp + retargeting
// ============================================================================

function sampleClip(clip, time) {
    const maxFrame = clip.frameCount - 1;
    let frameF = time * clip.fps;

    if (frameF <= 0) return retargetFrame(clip.frames[0]);
    if (frameF >= maxFrame) return retargetFrame(clip.frames[maxFrame]);

    const f0 = Math.floor(frameF);
    const f1 = Math.min(f0 + 1, maxFrame);
    const t = frameF - f0;

    if (t < 0.001) return retargetFrame(clip.frames[f0]);

    // Slerp deltas between frames, then retarget
    const frame0 = clip.frames[f0];
    const frame1 = clip.frames[f1];
    const interpolated = {};

    for (const bone in frame0) {
        const q0 = frame0[bone];
        const q1 = frame1[bone];
        if (!q1) { interpolated[bone] = q0; continue; }
        _qa.set(q0[0], q0[1], q0[2], q0[3]);
        _qb.set(q1[0], q1[1], q1[2], q1[3]);
        // Fix quaternion double-cover: ensure shortest path slerp
        if (_qa.dot(_qb) < 0) _qb.set(-_qb.x, -_qb.y, -_qb.z, -_qb.w);
        _qa.slerp(_qb, t);
        interpolated[bone] = [_qa.x, _qa.y, _qa.z, _qa.w];
    }

    // Interpolate Hips position
    const hp0 = frame0._hipsPos, hp1 = frame1._hipsPos;
    if (hp0 && hp1) {
        interpolated._hipsPos = [
            hp0[0] + (hp1[0] - hp0[0]) * t,
            hp0[1] + (hp1[1] - hp0[1]) * t,
            hp0[2] + (hp1[2] - hp0[2]) * t,
        ];
    } else if (hp0) {
        interpolated._hipsPos = hp0;
    }

    return retargetFrame(interpolated);
}

function blendPoses(poseA, poseB, t) {
    if (!poseA) return poseB;
    if (!poseB) return poseA;
    const result = {};
    for (const bone in poseB) {
        if (bone === '_hipsPos') continue;
        const qa = poseA[bone];
        const qb = poseB[bone];
        if (!qa) { result[bone] = qb; continue; }
        _qa.set(qa[0], qa[1], qa[2], qa[3]);
        _qb.set(qb[0], qb[1], qb[2], qb[3]);
        _qa.slerp(_qb, t);
        result[bone] = [_qa.x, _qa.y, _qa.z, _qa.w];
    }
    // Blend Hips position
    const hpA = poseA._hipsPos, hpB = poseB._hipsPos;
    if (hpA && hpB) {
        result._hipsPos = [
            hpA[0] + (hpB[0] - hpA[0]) * t,
            hpA[1] + (hpB[1] - hpA[1]) * t,
            hpA[2] + (hpB[2] - hpA[2]) * t,
        ];
    } else if (hpB) {
        result._hipsPos = hpB;
    }
    return result;
}

// ============================================================================
//  Playback
// ============================================================================

function playClipData(clip, { loop = false, fadeIn = CROSSFADE_SEC } = {}) {
    if (currentClip && fadeIn > 0) {
        snapshotPose = sampleClip(currentClip, Math.min(clipTime, currentClip.duration));
        crossfading = true;
        crossfadeTime = 0;
    } else {
        crossfading = false;
    }
    currentClip = clip;
    clipTime = 0;
    clipLoop = loop;
    loopsPlayed = 0;
}

// ============================================================================
//  Public API
// ============================================================================

function setGesture(functionName, opts = {}) {
    if (!ready) return;
    if (functionName === activeFunction && state !== State.IDLE) { pendingEnd = false; return; }

    const variants = functionMap.get(functionName);
    if (!variants?.length) { console.warn(`[AnimController] Unknown: ${functionName}`); return; }

    const variant = variants[Math.floor(Math.random() * variants.length)];
    activeFunction = functionName;
    activeVariant = variant;
    pendingEnd = false;
    loopsPlayed = 0;

    if (variant.looping) startLoopingGesture(variant);
    else startOneShotGesture(variant);
}

function endGesture() {
    if (state === State.LOOPING && activeVariant?.end_animation) pendingEnd = true;
    else goIdle();
}

function update(dt) {
    if (!ready || !currentClip) return;

    clipTime += dt;

    if (clipLoop && clipTime >= currentClip.duration) {
        clipTime = clipTime % currentClip.duration;
        loopsPlayed++;
    }

    if (!clipLoop && clipTime >= currentClip.duration) {
        onClipFinished();
        return;
    }

    if (state === State.LOOPING) {
        const maxRepeats = activeVariant?.max_repeats;
        if (maxRepeats && loopsPlayed >= maxRepeats) pendingEnd = true;
        if (pendingEnd) { pendingEnd = false; playEndClip(); return; }
    }

    let pose = sampleClip(currentClip, clipTime);

    if (crossfading) {
        crossfadeTime += dt;
        const t = Math.min(1, crossfadeTime / CROSSFADE_SEC);
        pose = blendPoses(snapshotPose, pose, t);
        if (t >= 1) crossfading = false;
    }

    if (window._applyBones) window._applyBones(pose);

    // Apply Hips position delta (FBX Z-up cm → VRM Y-up meters)
    if (pose._hipsPos) {
        const hp = pose._hipsPos;
        const vrm = window._getVRM?.();
        if (vrm) {
            // FBX X → VRM X, FBX Z (up) → VRM Y, FBX Y (fwd) → VRM -Z
            vrm.scene.position.set(
                hp[0] / 100,     // X: left/right
                hp[2] / 100,     // Y: up (from FBX Z)
                -hp[1] / 100     // Z: forward (from FBX -Y)
            );
        }
    }

    window._isPlayingMotion = true;

    if (state === State.IDLE) {
        fidgetTimer -= dt;
        if (fidgetTimer <= 0) { triggerFidget(); fidgetTimer = randomFidget(); }
    }
}

function isReady() { return ready; }

// ============================================================================
//  State machine
// ============================================================================

function onClipFinished() {
    if (state === State.STARTING) beginLoop(activeVariant?.loop_animation);
    else if (state === State.ENDING || state === State.PLAYING_ONCE) goIdle();
}

async function startLoopingGesture(v) {
    state = State.STARTING;
    if (v.start_animation) {
        const clip = await loadClip(v.start_animation);
        if (clip) { playClipData(clip, { loop: false }); return; }
    }
    beginLoop(v.loop_animation);
}

async function beginLoop(name) {
    state = State.LOOPING; loopsPlayed = 0;
    if (!name) { goIdle(); return; }
    const clip = await loadClip(name);
    if (clip) playClipData(clip, { loop: true }); else goIdle();
}

async function playEndClip() {
    const name = activeVariant?.end_animation;
    if (!name) { goIdle(); return; }
    state = State.ENDING;
    const clip = await loadClip(name);
    if (clip) playClipData(clip, { loop: false }); else goIdle();
}

async function startOneShotGesture(v) {
    state = State.PLAYING_ONCE;
    if (!v.animation) { goIdle(); return; }
    const clip = await loadClip(v.animation);
    if (clip) playClipData(clip, { loop: false }); else goIdle();
}

async function goIdle() {
    state = State.IDLE; activeFunction = null; activeVariant = null;
    const variants = functionMap.get(IDLE_FUNCTION);
    if (!variants?.length) return;
    const v = variants[Math.floor(Math.random() * variants.length)];
    const clipName = v.animation || v.loop_animation;
    if (!clipName) return;
    const clip = await loadClip(clipName);
    if (clip && state === State.IDLE) playClipData(clip, { loop: true });
}

async function triggerFidget() {
    if (state !== State.IDLE || !idleFunctions.length) return;
    const entry = idleFunctions[Math.floor(Math.random() * idleFunctions.length)];
    const variants = functionMap.get(entry.function);
    if (!variants) return;
    const variant = variants[Math.floor(Math.random() * variants.length)];
    const clipName = variant.animation || variant.loop_animation;
    if (!clipName) return;
    const clip = await loadClip(clipName);
    if (clip && state === State.IDLE) {
        state = State.PLAYING_ONCE;
        activeVariant = variant;
        playClipData(clip, { loop: false });
    }
}

function randomFidget() { return FIDGET_MIN + Math.random() * (FIDGET_MAX - FIDGET_MIN); }

// ============================================================================
//  Expose
// ============================================================================

window._animController = { setGesture, endGesture, update, isReady };
window._retargetFrame = retargetFrame;
