/*
 * Unity Editor Script — Export Humanoid AnimationClips to JSON
 *
 * Usage:
 *   1. Copy this file to your Unity project: Assets/Editor/ExportAnimations.cs
 *   2. In Unity menu bar: Tools → Export Animations to JSON
 *   3. Select the output folder when prompted
 *   4. JSON files appear in the chosen folder, one per clip
 *
 * Output format (per clip):
 * {
 *   "name": "KA_Combat_BareHands_Combo1",
 *   "fps": 30,
 *   "duration": 1.5,
 *   "frameCount": 45,
 *   "frames": [
 *     {
 *       "Spine": [qx, qy, qz, qw],
 *       "Chest": [qx, qy, qz, qw],
 *       "LeftUpperArm": [qx, qy, qz, qw],
 *       ...
 *     },
 *     ...
 *   ]
 * }
 *
 * The script uses Unity's Humanoid avatar system to sample bone rotations,
 * so it works with any Humanoid-rigged animation (Mixamo, Kawaii pack, etc.).
 * Bone names match VRM humanoid standard.
 */

using UnityEngine;
using UnityEditor;
using System.Collections.Generic;
using System.IO;

public class ExportAnimations : EditorWindow
{
    // Humanoid bone names that map directly to VRM humanoid bones
    static readonly HumanBodyBones[] EXPORT_BONES = new HumanBodyBones[]
    {
        HumanBodyBones.Hips,
        HumanBodyBones.Spine,
        HumanBodyBones.Chest,
        HumanBodyBones.UpperChest,
        HumanBodyBones.Neck,
        HumanBodyBones.Head,
        HumanBodyBones.LeftShoulder,
        HumanBodyBones.RightShoulder,
        HumanBodyBones.LeftUpperArm,
        HumanBodyBones.RightUpperArm,
        HumanBodyBones.LeftLowerArm,
        HumanBodyBones.RightLowerArm,
        HumanBodyBones.LeftHand,
        HumanBodyBones.RightHand,
        HumanBodyBones.LeftUpperLeg,
        HumanBodyBones.RightUpperLeg,
        HumanBodyBones.LeftLowerLeg,
        HumanBodyBones.RightLowerLeg,
        HumanBodyBones.LeftFoot,
        HumanBodyBones.RightFoot,
    };

    // Map Unity HumanBodyBones enum to VRM-compatible PascalCase names
    static readonly Dictionary<HumanBodyBones, string> BONE_NAMES = new Dictionary<HumanBodyBones, string>
    {
        { HumanBodyBones.Hips, "Hips" },
        { HumanBodyBones.Spine, "Spine" },
        { HumanBodyBones.Chest, "Chest" },
        { HumanBodyBones.UpperChest, "UpperChest" },
        { HumanBodyBones.Neck, "Neck" },
        { HumanBodyBones.Head, "Head" },
        { HumanBodyBones.LeftShoulder, "LeftShoulder" },
        { HumanBodyBones.RightShoulder, "RightShoulder" },
        { HumanBodyBones.LeftUpperArm, "LeftUpperArm" },
        { HumanBodyBones.RightUpperArm, "RightUpperArm" },
        { HumanBodyBones.LeftLowerArm, "LeftLowerArm" },
        { HumanBodyBones.RightLowerArm, "RightLowerArm" },
        { HumanBodyBones.LeftHand, "LeftHand" },
        { HumanBodyBones.RightHand, "RightHand" },
        { HumanBodyBones.LeftUpperLeg, "LeftUpperLeg" },
        { HumanBodyBones.RightUpperLeg, "RightUpperLeg" },
        { HumanBodyBones.LeftLowerLeg, "LeftLowerLeg" },
        { HumanBodyBones.RightLowerLeg, "RightLowerLeg" },
        { HumanBodyBones.LeftFoot, "LeftFoot" },
        { HumanBodyBones.RightFoot, "RightFoot" },
    };

    private int sampleFps = 30;
    private string searchFolder = "Assets";
    private bool upperBodyOnly = true;

    [MenuItem("Tools/Export Animations to JSON")]
    static void ShowWindow()
    {
        GetWindow<ExportAnimations>("Export Animations");
    }

    void OnGUI()
    {
        GUILayout.Label("Export Humanoid Animations to JSON", EditorStyles.boldLabel);
        GUILayout.Space(10);

        searchFolder = EditorGUILayout.TextField("Search Folder", searchFolder);
        sampleFps = EditorGUILayout.IntSlider("Sample FPS", sampleFps, 15, 60);
        upperBodyOnly = EditorGUILayout.Toggle("Upper Body Only", upperBodyOnly);

        GUILayout.Space(10);

        if (GUILayout.Button("Find & Export All AnimationClips"))
        {
            string outputDir = EditorUtility.OpenFolderPanel("Output Folder", "", "");
            if (!string.IsNullOrEmpty(outputDir))
            {
                ExportAll(outputDir);
            }
        }

        GUILayout.Space(5);
        EditorGUILayout.HelpBox(
            "This finds all AnimationClip assets under the search folder, " +
            "samples them at the given FPS using a temporary Humanoid avatar, " +
            "and writes one JSON file per clip.\n\n" +
            "The output is compatible with the ProjectParrot web VRM player.",
            MessageType.Info);
    }

    void ExportAll(string outputDir)
    {
        // Find all AnimationClip assets
        string[] guids = AssetDatabase.FindAssets("t:AnimationClip", new[] { searchFolder });
        if (guids.Length == 0)
        {
            EditorUtility.DisplayDialog("No Clips", "No AnimationClip assets found in " + searchFolder, "OK");
            return;
        }

        // We need a Humanoid avatar to sample animations.
        // Create a temporary GameObject with an Animator + a humanoid model.
        GameObject tempGo = CreateTempHumanoid();
        if (tempGo == null)
        {
            EditorUtility.DisplayDialog("Error",
                "Could not find a Humanoid model in the project to use as a sampling rig.\n" +
                "Import any Humanoid-rigged FBX (even the default Unity robot) and try again.", "OK");
            return;
        }

        Animator animator = tempGo.GetComponent<Animator>();
        int exported = 0;
        int skipped = 0;

        try
        {
            for (int i = 0; i < guids.Length; i++)
            {
                string path = AssetDatabase.GUIDToAssetPath(guids[i]);
                AnimationClip clip = AssetDatabase.LoadAssetAtPath<AnimationClip>(path);
                if (clip == null) continue;

                // Skip Unity default clips (preview, __preview__)
                if (clip.name.StartsWith("__")) { skipped++; continue; }

                EditorUtility.DisplayProgressBar("Exporting",
                    $"Clip {i + 1}/{guids.Length}: {clip.name}", (float)i / guids.Length);

                string json = SampleClipToJson(animator, clip, sampleFps);
                if (json == null) { skipped++; continue; }

                string safeName = clip.name.Replace(" ", "_").Replace("/", "_");
                string outPath = Path.Combine(outputDir, safeName + ".json");
                File.WriteAllText(outPath, json);
                exported++;
            }
        }
        finally
        {
            EditorUtility.ClearProgressBar();
            DestroyImmediate(tempGo);
        }

        EditorUtility.DisplayDialog("Done",
            $"Exported {exported} clips, skipped {skipped}.\nOutput: {outputDir}", "OK");
        Debug.Log($"[ExportAnimations] Exported {exported} clips to {outputDir}");
    }

    string SampleClipToJson(Animator animator, AnimationClip clip, int fps)
    {
        float duration = clip.length;
        if (duration <= 0) return null;

        int frameCount = Mathf.Max(1, Mathf.RoundToInt(duration * fps));
        float dt = duration / frameCount;

        // Determine which bones to export
        HumanBodyBones[] bones;
        if (upperBodyOnly)
        {
            bones = new HumanBodyBones[]
            {
                HumanBodyBones.Spine, HumanBodyBones.Chest, HumanBodyBones.UpperChest,
                HumanBodyBones.Neck, HumanBodyBones.Head,
                HumanBodyBones.LeftShoulder, HumanBodyBones.RightShoulder,
                HumanBodyBones.LeftUpperArm, HumanBodyBones.RightUpperArm,
                HumanBodyBones.LeftLowerArm, HumanBodyBones.RightLowerArm,
                HumanBodyBones.LeftHand, HumanBodyBones.RightHand,
            };
        }
        else
        {
            bones = EXPORT_BONES;
        }

        // Capture rest-pose localRotation for each bone BEFORE animation plays.
        // GetBoneTransform().localRotation returns the RAW skeleton-specific
        // rotation (not normalized). By computing Inverse(rest) * animated,
        // we strip the skeleton's rest orientation and export pure animation
        // deltas. These deltas apply directly to VRM normalized bones (which
        // have identity quaternion at rest).
        var restPose = new Dictionary<HumanBodyBones, Quaternion>();
        foreach (var bone in bones)
        {
            Transform t = animator.GetBoneTransform(bone);
            if (t != null) restPose[bone] = t.localRotation;
        }

        // Build frames array
        var sb = new System.Text.StringBuilder();
        sb.Append("{\n");
        sb.AppendFormat("  \"name\": \"{0}\",\n", clip.name);
        sb.AppendFormat("  \"fps\": {0},\n", fps);
        sb.AppendFormat("  \"duration\": {0:F4},\n", duration);
        sb.AppendFormat("  \"frameCount\": {0},\n", frameCount);
        sb.Append("  \"frames\": [\n");

        for (int f = 0; f < frameCount; f++)
        {
            float time = f * dt;
            clip.SampleAnimation(animator.gameObject, time);

            sb.Append("    {");
            bool first = true;

            foreach (var bone in bones)
            {
                Transform boneTransform = animator.GetBoneTransform(bone);
                if (boneTransform == null) continue;
                if (!restPose.ContainsKey(bone)) continue;

                string boneName = BONE_NAMES[bone];

                // Delta = Inverse(restPose) * animatedPose
                // Strips the skeleton's rest orientation, leaving only the
                // animation change from rest. At rest, delta = identity.
                Quaternion delta = Quaternion.Inverse(restPose[bone]) * boneTransform.localRotation;

                // Unity left-handed → three.js right-handed: negate X and Y.
                float qx = -delta.x;
                float qy = -delta.y;
                float qz = delta.z;
                float qw = delta.w;

                if (!first) sb.Append(",");
                sb.AppendFormat("\"{0}\":[{1:F6},{2:F6},{3:F6},{4:F6}]",
                    boneName, qx, qy, qz, qw);
                first = false;
            }

            sb.Append("}");
            if (f < frameCount - 1) sb.Append(",");
            sb.Append("\n");
        }

        sb.Append("  ]\n}");
        return sb.ToString();
    }

    /// <summary>
    /// Create a temporary GameObject with a Humanoid Animator for sampling.
    /// Searches the project for any FBX/model with a Humanoid avatar.
    /// </summary>
    GameObject CreateTempHumanoid()
    {
        // Search for any model with a Humanoid avatar
        string[] modelGuids = AssetDatabase.FindAssets("t:Model");
        foreach (string guid in modelGuids)
        {
            string path = AssetDatabase.GUIDToAssetPath(guid);
            ModelImporter importer = AssetImporter.GetAtPath(path) as ModelImporter;
            if (importer == null || importer.animationType != ModelImporterAnimationType.Human)
                continue;

            GameObject prefab = AssetDatabase.LoadAssetAtPath<GameObject>(path);
            if (prefab == null) continue;

            GameObject instance = Instantiate(prefab);
            instance.name = "__AnimExportTemp";
            instance.hideFlags = HideFlags.HideAndDontSave;

            Animator animator = instance.GetComponent<Animator>();
            if (animator != null && animator.avatar != null && animator.avatar.isHuman)
            {
                return instance;
            }

            DestroyImmediate(instance);
        }

        return null;
    }
}
