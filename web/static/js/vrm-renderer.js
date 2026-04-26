/**
 * vrm-renderer.js — Three.js VRM rendering module for Mocha web app.
 *
 * ES module that handles scene setup, VRM loading, bone/viseme/emotion
 * application, drag-and-drop, auto-loading, audio analyser, and responsive
 * canvas resizing.
 *
 * Exposes functions on `window` so the non-module app.js can call them.
 */

import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

// ============================================================================
//  Constants
// ============================================================================

const VRM_CDN = 'https://cdn.jsdelivr.net/npm/@pixiv/three-vrm@3.3.3/lib/three-vrm.module.min.js';

/** PascalCase bone names from the server mapped to camelCase for three-vrm v3 */
const BONE_MAP = {
    // Core
    'Hips': 'hips', 'Spine': 'spine', 'Chest': 'chest',
    'UpperChest': 'upperChest', 'Neck': 'neck', 'Head': 'head',
    // Arms
    'LeftShoulder': 'leftShoulder', 'RightShoulder': 'rightShoulder',
    'LeftUpperArm': 'leftUpperArm', 'RightUpperArm': 'rightUpperArm',
    'LeftLowerArm': 'leftLowerArm', 'RightLowerArm': 'rightLowerArm',
    'LeftHand': 'leftHand', 'RightHand': 'rightHand',
    // Legs
    'LeftUpperLeg': 'leftUpperLeg', 'RightUpperLeg': 'rightUpperLeg',
    'LeftLowerLeg': 'leftLowerLeg', 'RightLowerLeg': 'rightLowerLeg',
    'LeftFoot': 'leftFoot', 'RightFoot': 'rightFoot',
    'LeftToes': 'leftToes', 'RightToes': 'rightToes',
    // Left fingers
    'LeftThumbMetacarpal': 'leftThumbMetacarpal',
    'LeftThumbProximal': 'leftThumbProximal', 'LeftThumbDistal': 'leftThumbDistal',
    'LeftIndexProximal': 'leftIndexProximal', 'LeftIndexIntermediate': 'leftIndexIntermediate',
    'LeftIndexDistal': 'leftIndexDistal',
    'LeftMiddleProximal': 'leftMiddleProximal', 'LeftMiddleIntermediate': 'leftMiddleIntermediate',
    'LeftMiddleDistal': 'leftMiddleDistal',
    'LeftRingProximal': 'leftRingProximal', 'LeftRingIntermediate': 'leftRingIntermediate',
    'LeftRingDistal': 'leftRingDistal',
    'LeftLittleProximal': 'leftLittleProximal', 'LeftLittleIntermediate': 'leftLittleIntermediate',
    'LeftLittleDistal': 'leftLittleDistal',
    // Right fingers
    'RightThumbMetacarpal': 'rightThumbMetacarpal',
    'RightThumbProximal': 'rightThumbProximal', 'RightThumbDistal': 'rightThumbDistal',
    'RightIndexProximal': 'rightIndexProximal', 'RightIndexIntermediate': 'rightIndexIntermediate',
    'RightIndexDistal': 'rightIndexDistal',
    'RightMiddleProximal': 'rightMiddleProximal', 'RightMiddleIntermediate': 'rightMiddleIntermediate',
    'RightMiddleDistal': 'rightMiddleDistal',
    'RightRingProximal': 'rightRingProximal', 'RightRingIntermediate': 'rightRingIntermediate',
    'RightRingDistal': 'rightRingDistal',
    'RightLittleProximal': 'rightLittleProximal', 'RightLittleIntermediate': 'rightLittleIntermediate',
    'RightLittleDistal': 'rightLittleDistal',
};

/** Emotion ID to VRM expression preset + weight */
const EMOTION_TO_VRM = {
    neutral:    { preset: 'neutral',   weight: 0.0 },
    happy:      { preset: 'happy',     weight: 0.8 },
    excited:    { preset: 'happy',     weight: 1.0 },
    thinking:   { preset: 'neutral',   weight: 0.0 },
    sad:        { preset: 'sad',       weight: 0.7 },
    surprised:  { preset: 'surprised', weight: 0.9 },
    playful:    { preset: 'happy',     weight: 0.7 },
    empathetic: { preset: 'sad',       weight: 0.3 },
};

/** Five-channel viseme names */
const VISEME_NAMES = ['aa', 'ih', 'ou', 'ee', 'oh'];

// ============================================================================
//  Module state
// ============================================================================

let VRMModule = null;
let scene, camera, renderer, controls;
let vrm = null;
let currentEmotionPreset = null;

// ============================================================================
//  VRM library loader (dynamic import from CDN)
// ============================================================================

async function loadVRMLib() {
    if (!VRMModule) {
        VRMModule = await import(VRM_CDN);
    }
    return VRMModule;
}

// ============================================================================
//  Three.js scene setup
// ============================================================================

function initThree() {
    const canvas = document.getElementById('canvas3d');
    const area = document.getElementById('canvasArea');
    const w = area.clientWidth;
    const h = area.clientHeight;

    // Scene — transparent so the canvas-area CSS background shows through.
    // The background photo is applied as a normal 2D CSS image on #canvasArea
    // (via setSceneBackground below); this avoids the fisheye warp you'd get
    // from treating a flat photo as an equirectangular 360° texture.
    scene = new THREE.Scene();
    scene.background = null;

    // Apply saved-or-default background image and tuning to #bgLayer.
    const savedBg = (() => {
        try { return localStorage.getItem('parrot.bg'); } catch { return null; }
    })();
    const savedStyles = (() => {
        try {
            const raw = localStorage.getItem('parrot.bg_settings');
            return raw ? JSON.parse(raw) : null;
        } catch { return null; }
    })();
    setSceneBackground(savedBg || null);
    if (savedStyles) setSceneBackgroundStyles(savedStyles);

    // Camera
    camera = new THREE.PerspectiveCamera(25, w / h, 0.1, 50);
    camera.position.set(0, 1.25, 3.0);

    // Renderer — alpha: true so transparent clear lets CSS background show.
    renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
    renderer.setSize(w, h);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.outputColorSpace = THREE.SRGBColorSpace;

    // Lights
    scene.add(new THREE.AmbientLight(0xffffff, 1.0));

    const dirLight = new THREE.DirectionalLight(0xffffff, 1.0);
    dirLight.position.set(2, 3, 4);
    scene.add(dirLight);

    const backLight = new THREE.DirectionalLight(0x8888ff, 0.3);
    backLight.position.set(-2, 1, -3);
    scene.add(backLight);

    // Orbit controls — disabled by default (unlocked via Ctrl+B panel)
    controls = new OrbitControls(camera, canvas);
    controls.target.set(0, 1.0, 0);
    controls.enableDamping = true;
    controls.dampingFactor = 0.1;
    controls.enabled = false;  // locked until Ctrl+B opens
    controls.update();

    // Responsive resizing via ResizeObserver.
    // Delays resize until CSS transitions finish (sidebar collapse/expand)
    // to prevent Mocha from flashing during the animation.
    let _resizeTimer = null;
    let _lastW = area.clientWidth;
    let _lastH = area.clientHeight;

    function _doResize() {
        const w2 = area.clientWidth;
        const h2 = area.clientHeight;
        if (w2 && h2 && (w2 !== _lastW || h2 !== _lastH)) {
            _lastW = w2; _lastH = h2;
            camera.aspect = w2 / h2;
            camera.updateProjectionMatrix();
            renderer.setSize(w2, h2);
        }
    }

    // Canvas is now always fullscreen (position:fixed inset:0), so it only
    // resizes on viewport change, not sidebar toggle. Still observe for safety.
    const ro = new ResizeObserver(() => {
        if (_resizeTimer) clearTimeout(_resizeTimer);
        _resizeTimer = setTimeout(_doResize, 100);
    });
    ro.observe(area);

    // Render loop with idle animation + smooth camera tracking
    let idleTime = 0;
    const clock = new THREE.Clock();

    // Smooth camera tracking: lerp toward ideal position every frame
    const _camLerp = 0.03;  // lower = smoother/slower (0.03 ≈ 0.5s settle)

    // Persistent calibration offset. Ctrl+B calibration writes into this so
    // manual tweaks survive layout-driven camera changes. Zero = lerp tracks
    // PanelManager's getIdealCamera() exactly; non-zero = track ideal + off.
    window._camOffset = window._camOffset || {
        camX: 0, camY: 0, camZ: 0, tgtX: 0, tgtY: 0, tgtZ: 0,
    };

    function _lerpCam(dt) {
        // Skip if orbit controls are unlocked (user is manually adjusting)
        if (controls.enabled) return;
        // Skip if presentation.js is running a tween
        if (window._presentation?._isTweening?.()) return;

        const pm = window.PanelManager;
        if (!pm) return;

        const ideal = pm.getIdealCamera();
        if (!ideal) return;

        const off = window._camOffset;
        const tCamX = ideal.camX + off.camX;
        const tCamY = ideal.camY + off.camY;
        const tCamZ = ideal.camZ + off.camZ;
        const tTgtX = ideal.tgtX + off.tgtX;
        const tTgtY = ideal.tgtY + off.tgtY;
        const tTgtZ = ideal.tgtZ + off.tgtZ;

        // Lerp camera position and target toward (ideal + calibration offset)
        camera.position.x += (tCamX - camera.position.x) * _camLerp;
        camera.position.y += (tCamY - camera.position.y) * _camLerp;
        camera.position.z += (tCamZ - camera.position.z) * _camLerp;
        controls.target.x += (tTgtX - controls.target.x) * _camLerp;
        controls.target.y += (tTgtY - controls.target.y) * _camLerp;
        controls.target.z += (tTgtZ - controls.target.z) * _camLerp;

        // Rotate Mocha so she faces the camera's X/Z position. Without this,
        // when a panel shifts the camera off-center, Mocha stays pointed at
        // world origin and perspective twists her shoulders "away" from the
        // content. Facing the camera means she always reads as turned toward
        // the viewer (and therefore toward the visible browser center).
        if (vrm) {
            const dx = camera.position.x - vrm.scene.position.x;
            const dz = camera.position.z - vrm.scene.position.z;
            // Subtle only: scale the full face-the-camera angle by 0.5 so she
            // tilts partway instead of fully snapping to face the camera.
            const FACING_STRENGTH = 0.5;
            const targetRotY = Math.atan2(dx, dz) * FACING_STRENGTH;
            // Shortest-path lerp for rotation
            let diff = targetRotY - vrm.scene.rotation.y;
            while (diff > Math.PI) diff -= 2 * Math.PI;
            while (diff < -Math.PI) diff += 2 * Math.PI;
            vrm.scene.rotation.y += diff * _camLerp;
        }
    }

    function loop() {
        requestAnimationFrame(loop);
        const dt = clock.getDelta();
        idleTime += dt;
        _lerpCam(dt);
        controls.update();

        // Animation controller FIRST (sets bone quaternions from JSON clips)
        if (vrm && window._animController && window._animController.isReady()) {
            window._animController.update(dt);
        }
        // VRM update AFTER (spring bones compute collisions on current pose)
        if (vrm) vrm.update(dt);
        // Skeleton debug overlay
        if (skeletonMode) updateSkeletonViz();
        renderer.render(scene, camera);
    }
    loop();
}

// ============================================================================
//  VRM model loading
// ============================================================================

async function loadVRMFile(file) {
    const debugBar = document.getElementById('debugBar');
    if (debugBar) debugBar.textContent = 'Loading VRM...';

    const { VRMLoaderPlugin, VRMUtils } = await loadVRMLib();
    const url = URL.createObjectURL(file);
    const loader = new GLTFLoader();
    loader.register(p => new VRMLoaderPlugin(p));

    try {
        const gltf = await loader.loadAsync(url);
        URL.revokeObjectURL(url);

        // Remove previous model
        if (vrm) scene.remove(vrm.scene);

        vrm = gltf.userData.vrm;
        VRMUtils.removeUnnecessaryVertices(vrm.scene);
        if (VRMUtils.combineSkeletons) {
            VRMUtils.combineSkeletons(vrm.scene);
        } else {
            VRMUtils.removeUnnecessaryJoints(vrm.scene);
        }
        scene.add(vrm.scene);

        // Hide drop overlay, show clear button
        document.getElementById('dropOverlay').style.display = 'none';
        document.getElementById('btnClearVRM').style.display = '';

        // Log available expressions
        if (vrm.expressionManager) {
            console.log('VRM expressions:',
                vrm.expressionManager.expressions.map(e => e.expressionName));
        }

        if (debugBar) debugBar.textContent = 'VRM loaded';
    } catch (e) {
        console.error(e);
        if (debugBar) debugBar.textContent = 'VRM load failed: ' + e.message;
    }
}

// ============================================================================
//  Auto-load default model from server
// ============================================================================

async function autoLoadDefault() {
    // Try the per-user active VRM first (covers logged-in users with a custom
    // model), fall back to the global default if that endpoint isn't available
    // or returns an error.
    const headers = (typeof authHeaders === 'function') ? authHeaders() : {};
    for (const url of ['/api/user/vrm', '/api/default-model']) {
        try {
            const resp = await fetch(url, { headers });
            if (resp.ok) {
                const blob = await resp.blob();
                const file = new File([blob], 'character.vrm');
                await loadVRMFile(file);
                return;
            }
        } catch (e) { /* try next */ }
    }
    console.log('No default VRM model available');
}

/** Load a VRM by URL. Used by the Avatar tab when the user activates a
 *  different model in their library. */
async function loadVRMFromUrl(url) {
    const headers = (typeof authHeaders === 'function') ? authHeaders() : {};
    const resp = await fetch(url, { headers });
    if (!resp.ok) throw new Error('Failed to fetch VRM: ' + resp.status);
    const blob = await resp.blob();
    const file = new File([blob], 'character.vrm');
    await loadVRMFile(file);
}
window.loadVRMFromUrl = loadVRMFromUrl;

// Track whether motion playback is active
window._isPlayingMotion = false;

// ============================================================================
//  Bone quaternion application
// ============================================================================

/**
 * Apply per-frame bone quaternions.
 * @param {Object} boneFrame - Map of PascalCase bone name to [qx, qy, qz, qw]
 */
function applyBones(boneFrame) {
    if (!vrm) return;
    for (const [name, q] of Object.entries(boneFrame)) {
        const vrmName = BONE_MAP[name];
        if (!vrmName) continue;
        const bone = vrm.humanoid.getNormalizedBoneNode(vrmName);
        if (bone) bone.quaternion.set(q[0], q[1], q[2], q[3]);
    }
}

/**
 * Reset all mapped bones to identity quaternion.
 */
function resetBones() {
    if (!vrm) return;
    for (const vrmName of Object.values(BONE_MAP)) {
        const bone = vrm.humanoid.getNormalizedBoneNode(vrmName);
        if (bone) bone.quaternion.set(0, 0, 0, 1);
    }
}

// ============================================================================
//  Viseme / lip-sync blend shapes
// ============================================================================

/**
 * Apply 5-channel viseme weights.
 * @param {number[]} weights - [aa, ih, ou, ee, oh] in 0..1
 */
function applyVisemes(weights) {
    if (!vrm || !vrm.expressionManager) return;
    for (let i = 0; i < VISEME_NAMES.length; i++) {
        try {
            vrm.expressionManager.setValue(VISEME_NAMES[i], weights[i] || 0);
        } catch (e) { /* expression may not exist on this model */ }
    }
}

/**
 * Zero all viseme blend shapes.
 */
function clearLipSync() {
    if (!vrm || !vrm.expressionManager) return;
    for (const name of VISEME_NAMES) {
        try {
            vrm.expressionManager.setValue(name, 0);
        } catch (e) { /* ignore */ }
    }
}

// ============================================================================
//  Emotion expression application
// ============================================================================

/**
 * Apply an emotion expression preset to the VRM model.
 * @param {string} emotionId - One of the EMOTION_TO_VRM keys
 */
function applyEmotion(emotionId) {
    if (!vrm || !vrm.expressionManager) return;

    // Clear previous emotion
    if (currentEmotionPreset) {
        try {
            vrm.expressionManager.setValue(currentEmotionPreset, 0);
        } catch (e) { /* ignore */ }
    }

    const emo = EMOTION_TO_VRM[emotionId] || { preset: 'neutral', weight: 0 };
    if (emo.preset !== 'neutral') {
        try {
            vrm.expressionManager.setValue(emo.preset, emo.weight);
        } catch (e) { /* ignore */ }
    }
    currentEmotionPreset = emo.preset;
}

// ============================================================================
//  Scene background (equirectangular image)
// ============================================================================

/**
 * Swap the background image. Applied as a CSS background on #bgLayer,
 * which sits behind the transparent canvas (no equirectangular warp, and
 * filters don't affect Mocha).
 * @param {string} src URL or data URL
 */
function setSceneBackground(src) {
    const layer = document.getElementById('bgLayer');
    if (!layer) return;
    layer.style.backgroundImage = src ? `url("${src.replace(/"/g, '\\"')}")` : 'none';
}

/**
 * Apply visual tuning to the background layer.
 * @param {{fit?:string,posX?:number,posY?:number,blur?:number,brightness?:number}} s
 *   fit: 'cover' | 'contain' | 'fill' | 'auto'
 *   posX/posY: 0..100 percent
 *   blur: pixels (0..20 typical)
 *   brightness: 0..1 multiplier (1 = normal, 0.5 = half as bright)
 */
function setSceneBackgroundStyles(s = {}) {
    const layer = document.getElementById('bgLayer');
    if (!layer) return;
    const fit = s.fit || 'cover';
    const posX = (s.posX ?? 50);
    const posY = (s.posY ?? 50);
    const blur = (s.blur ?? 0);
    const brightness = (s.brightness ?? 1);

    layer.style.backgroundSize = (fit === 'fill') ? '100% 100%'
        : (fit === 'auto') ? 'auto' : fit;
    layer.style.backgroundPosition = `${posX}% ${posY}%`;

    const filters = [];
    if (blur > 0) filters.push(`blur(${blur}px)`);
    if (brightness !== 1) filters.push(`brightness(${brightness})`);
    layer.style.filter = filters.join(' ');
}

// ============================================================================
//  Audio analyser for browser-side lip sync fallback
// ============================================================================

/**
 * Connect an audio analyser node for browser-side lip sync.
 * @param {AudioContext} audioCtx
 * @param {AudioBufferSourceNode} sourceNode
 * @returns {{ analyser: AnalyserNode, data: Uint8Array }}
 */
function setupAnalyser(audioCtx, sourceNode) {
    const analyser = audioCtx.createAnalyser();
    analyser.fftSize = 2048;
    analyser.smoothingTimeConstant = 0.6;
    const data = new Uint8Array(analyser.frequencyBinCount);
    sourceNode.connect(analyser);
    analyser.connect(audioCtx.destination);
    return { analyser, data };
}

// ============================================================================
//  Skeleton debug visualization
// ============================================================================

let skeletonMode = false;
let skeletonGroup = null;

const SKELETON_BONES = Object.values(BONE_MAP);

const SKELETON_CONNECTIONS = [
    // Spine
    ['hips','spine'],['spine','chest'],['chest','upperChest'],
    ['upperChest','neck'],['neck','head'],
    // Left arm
    ['upperChest','leftShoulder'],['leftShoulder','leftUpperArm'],
    ['leftUpperArm','leftLowerArm'],['leftLowerArm','leftHand'],
    // Right arm
    ['upperChest','rightShoulder'],['rightShoulder','rightUpperArm'],
    ['rightUpperArm','rightLowerArm'],['rightLowerArm','rightHand'],
    // Left leg
    ['hips','leftUpperLeg'],['leftUpperLeg','leftLowerLeg'],
    ['leftLowerLeg','leftFoot'],['leftFoot','leftToes'],
    // Right leg
    ['hips','rightUpperLeg'],['rightUpperLeg','rightLowerLeg'],
    ['rightLowerLeg','rightFoot'],['rightFoot','rightToes'],
    // Left fingers
    ['leftHand','leftThumbMetacarpal'],['leftThumbMetacarpal','leftThumbProximal'],['leftThumbProximal','leftThumbDistal'],
    ['leftHand','leftIndexProximal'],['leftIndexProximal','leftIndexIntermediate'],['leftIndexIntermediate','leftIndexDistal'],
    ['leftHand','leftMiddleProximal'],['leftMiddleProximal','leftMiddleIntermediate'],['leftMiddleIntermediate','leftMiddleDistal'],
    ['leftHand','leftRingProximal'],['leftRingProximal','leftRingIntermediate'],['leftRingIntermediate','leftRingDistal'],
    ['leftHand','leftLittleProximal'],['leftLittleProximal','leftLittleIntermediate'],['leftLittleIntermediate','leftLittleDistal'],
    // Right fingers
    ['rightHand','rightThumbMetacarpal'],['rightThumbMetacarpal','rightThumbProximal'],['rightThumbProximal','rightThumbDistal'],
    ['rightHand','rightIndexProximal'],['rightIndexProximal','rightIndexIntermediate'],['rightIndexIntermediate','rightIndexDistal'],
    ['rightHand','rightMiddleProximal'],['rightMiddleProximal','rightMiddleIntermediate'],['rightMiddleIntermediate','rightMiddleDistal'],
    ['rightHand','rightRingProximal'],['rightRingProximal','rightRingIntermediate'],['rightRingIntermediate','rightRingDistal'],
    ['rightHand','rightLittleProximal'],['rightLittleProximal','rightLittleIntermediate'],['rightLittleIntermediate','rightLittleDistal'],
];

function updateSkeletonViz() {
    if (!vrm || !skeletonMode) return;

    if (!skeletonGroup) {
        skeletonGroup = new THREE.Group();
        skeletonGroup.name = '__skeletonDebug';
        skeletonGroup.renderOrder = 9999;
        scene.add(skeletonGroup);
    }

    // Clear previous frame
    while (skeletonGroup.children.length) skeletonGroup.remove(skeletonGroup.children[0]);

    const positions = {};
    const wPos = new THREE.Vector3();

    // Collect world positions for each bone
    for (const boneName of SKELETON_BONES) {
        // Try normalized first, fall back to raw
        const node = vrm.humanoid.getNormalizedBoneNode(boneName)
                  || vrm.humanoid.getRawBoneNode(boneName);
        if (node) {
            node.getWorldPosition(wPos);
            positions[boneName] = wPos.clone();
        }
    }

    // Draw bones as lines
    const lineMat = new THREE.LineBasicMaterial({ color: 0x00ff88, linewidth: 2, depthTest: false });
    for (const [a, b] of SKELETON_CONNECTIONS) {
        if (!positions[a] || !positions[b]) continue;
        const geo = new THREE.BufferGeometry().setFromPoints([positions[a], positions[b]]);
        const line = new THREE.Line(geo, lineMat);
        line.renderOrder = 9999;
        skeletonGroup.add(line);
    }

    // Draw joints as small spheres with axis arrows
    const jointGeo = new THREE.SphereGeometry(0.012, 6, 6);
    const jointMat = new THREE.MeshBasicMaterial({ color: 0xffff00, depthTest: false });

    for (const boneName of SKELETON_BONES) {
        const node = vrm.humanoid.getNormalizedBoneNode(boneName)
                  || vrm.humanoid.getRawBoneNode(boneName);
        if (!node || !positions[boneName]) continue;

        // Joint sphere
        const sphere = new THREE.Mesh(jointGeo, jointMat);
        sphere.position.copy(positions[boneName]);
        sphere.renderOrder = 10000;
        skeletonGroup.add(sphere);

        // Axis arrows (X=red, Y=green, Z=blue) — 3cm each
        const axisLen = 0.03;
        const wq = new THREE.Quaternion();
        node.getWorldQuaternion(wq);

        const axes = [
            { dir: new THREE.Vector3(1, 0, 0), color: 0xff0000 },
            { dir: new THREE.Vector3(0, 1, 0), color: 0x00ff00 },
            { dir: new THREE.Vector3(0, 0, 1), color: 0x0000ff },
        ];

        for (const { dir, color } of axes) {
            const d = dir.clone().applyQuaternion(wq).multiplyScalar(axisLen);
            const start = positions[boneName];
            const end = start.clone().add(d);
            const geo = new THREE.BufferGeometry().setFromPoints([start, end]);
            const mat = new THREE.LineBasicMaterial({ color, depthTest: false, linewidth: 2 });
            const line = new THREE.Line(geo, mat);
            line.renderOrder = 10001;
            skeletonGroup.add(line);
        }
    }
}

function toggleSkeletonMode() {
    skeletonMode = !skeletonMode;
    if (vrm) {
        vrm.scene.traverse(child => {
            if (child.isMesh || child.isSkinnedMesh) {
                child.visible = !skeletonMode;
            }
        });
    }
    if (!skeletonMode && skeletonGroup) {
        scene.remove(skeletonGroup);
        skeletonGroup = null;
    }
    console.log(`[Skeleton] ${skeletonMode ? 'ON' : 'OFF'}`);
}

window._toggleSkeleton = toggleSkeletonMode;

// ============================================================================
//  Clear VRM model
// ============================================================================

function clearVRM() {
    if (vrm) {
        scene.remove(vrm.scene);
        vrm = null;
    }
    document.getElementById('dropOverlay').style.display = '';
    document.getElementById('btnClearVRM').style.display = 'none';
}

/**
 * Load a VRM model from a File object (used by file input and drag-and-drop).
 * @param {File} file
 */
async function loadVRMFromFile(file) {
    await loadVRMFile(file);
}

// ============================================================================
//  (Drag-and-drop on canvas removed — VRM uploads now live in
//   Settings → Avatar tab, see web/static/js/avatar.js. The canvas
//   #dropOverlay element only shows the initial "Loading…" message and is
//   hidden once the model is in. Keeping the no-op stub here so any callers
//   that still invoke initDragAndDrop() don't throw.)
// ============================================================================

function initDragAndDrop() { /* intentionally empty */ }

// ============================================================================
//  File input helper (called from onclick in HTML)
// ============================================================================

function loadVRMFromInput(input) {
    if (input.files[0]) loadVRMFile(input.files[0]);
}

// ============================================================================
//  Expose functions to window for non-module app.js
// ============================================================================

window._applyBones = applyBones;
window._applyVisemes = applyVisemes;

// Expose VRM internals for animation controller
window._getVRM = () => vrm;
window._getScene = () => scene;
/** Debug: dump all VRM raw bone names to console */
window._dumpBoneNames = () => {
    if (!vrm) { console.log('VRM not loaded'); return; }
    const names = [
        'hips','spine','chest','upperChest','neck','head',
        'leftShoulder','rightShoulder','leftUpperArm','rightUpperArm',
        'leftLowerArm','rightLowerArm','leftHand','rightHand',
        'leftUpperLeg','rightUpperLeg','leftLowerLeg','rightLowerLeg',
        'leftFoot','rightFoot',
    ];
    for (const n of names) {
        const raw = vrm.humanoid.getRawBoneNode(n);
        const norm = vrm.humanoid.getNormalizedBoneNode(n);
        console.log(`${n}: raw=${raw?.name || 'NONE'}, norm=${norm?.name || 'NONE'}`);
    }
};
window._applyEmotion = applyEmotion;
window._resetBones = resetBones;
window._clearLipSync = clearLipSync;
window._setupAnalyser = setupAnalyser;
window._clearVRM = clearVRM;
window._loadVRMFromFile = loadVRMFromFile;
window._loadVRMFromInput = loadVRMFromInput;
window._setSceneBackground = setSceneBackground;
window._setSceneBackgroundStyles = setSceneBackgroundStyles;

// Camera & controls access for calibration panel
window._getCamera = () => camera;
window._getControls = () => controls;


// Also expose the emotion map so app.js can reference it
window.EMOTION_TO_VRM = EMOTION_TO_VRM;

// ============================================================================
//  Initialization
// ============================================================================

initThree();
initDragAndDrop();
autoLoadDefault();
