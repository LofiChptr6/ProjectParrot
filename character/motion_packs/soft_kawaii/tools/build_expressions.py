"""Edit only VRM expression bindings. Preserve meshes/textures/rig binary verbatim."""
import copy
import hashlib
import json
import shutil
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
PACK = ROOT / 'character/motion_packs/soft_kawaii'
target = ROOT/'character/Mocha.vrm'
backup = ROOT/'web/static/animations/Mocha_before_expressions.vrm'
if not backup.exists():
    # A fresh clone already contains the revised model but not the ignored
    # backup. Restore its original expression JSON from the tracked recipe.
    saved = PACK/'expression_recipes.json'
    if saved.exists():
        current = target.read_bytes()
        size, chunk_kind = struct.unpack_from('<II', current, 12)
        baseline = json.loads(current[20:20+size])
        baseline['extensions']['VRMC_vrm']['expressions'] = json.loads(saved.read_text(encoding='utf-8'))['original_expressions']
        encoded = json.dumps(baseline, separators=(',',':'), ensure_ascii=False).encode('utf-8')
        encoded += b' ' * ((-len(encoded)) % 4)
        remainder = current[20+size:]
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(struct.pack('<4sII',b'glTF',2,20+len(encoded)+len(remainder)) + struct.pack('<II',len(encoded),chunk_kind) + encoded + remainder)
    else:
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, backup)
blob = backup.read_bytes()
assert blob[:4] == b'glTF'
length, kind = struct.unpack_from('<II', blob, 12)
assert kind == 0x4E4F534A
doc = json.loads(blob[20:20+length])
tail = blob[20+length:]
expressions = doc['extensions']['VRMC_vrm']['expressions']
original = copy.deepcopy(expressions)
node = original['preset']['happy']['morphTargetBinds'][0]['node']
mesh = doc['meshes'][doc['nodes'][node]['mesh']]
names = mesh['extras']['targetNames']
recipes = {
    'smile': {'Fcl_MTH_Fun': .72, 'Fcl_BRW_Joy': .22, 'Fcl_EYE_Fun': .08},
    'warm_smile': {'Fcl_MTH_Fun': .58, 'Fcl_BRW_Fun': .18, 'Fcl_EYE_Fun': .06},
    'bright_smile': {'Fcl_MTH_Joy': .85, 'Fcl_BRW_Joy': .4, 'Fcl_EYE_Joy': .14},
    'bashful': {'Fcl_MTH_Fun': .4, 'Fcl_BRW_Sorrow': .12, 'Fcl_EYE_Fun': .18},
    'amused': {'Fcl_MTH_Fun': .63, 'Fcl_BRW_Fun': .4, 'Fcl_EYE_Fun': .30},
    'playful_wink': {'Fcl_MTH_Fun': .6, 'Fcl_BRW_Fun': .25, 'Fcl_EYE_Joy_R': .92},
    'tender': {'Fcl_MTH_Fun': .30, 'Fcl_BRW_Sorrow': .18, 'Fcl_EYE_Fun': .10},
    'pout': {'Fcl_MTH_Angry': .28, 'Fcl_MTH_Small': .3, 'Fcl_BRW_Angry': .24, 'Fcl_EYE_Angry': .12},
    'sleepy': {'Fcl_EYE_Close': .46, 'Fcl_BRW_Fun': .08, 'Fcl_MTH_Neutral': .12},
    'kiss': {'Fcl_MTH_U': .32, 'Fcl_MTH_Small': .22, 'Fcl_EYE_Fun': .12},
    'curious': {'Fcl_BRW_Surprised': .27, 'Fcl_EYE_Surprised': .15, 'Fcl_MTH_Neutral': .15},
}
def expression(recipe):
    return {'morphTargetBinds': [{'node':node,'index':names.index(name),'weight':weight} for name,weight in recipe.items()],
            'isBinary':False,'overrideBlink':'none','overrideLookAt':'none','overrideMouth':'none'}

# Keep the original broad, closed-eye happy preset verbatim. The quieter
# open-eye expression is independently selectable as the custom 'smile'.
custom = expressions.setdefault('custom', {})
for name, recipe in recipes.items():
    custom[name] = expression(recipe)
custom['laugh_closed'] = copy.deepcopy(original['preset']['happy'])
custom['laugh_closed']['overrideBlink'] = 'blend'
custom['playful_wink']['overrideBlink'] = 'blend'
encoded = json.dumps(doc, separators=(',',':'), ensure_ascii=False).encode('utf-8')
encoded += b' ' * ((-len(encoded)) % 4)
result = struct.pack('<4sII',b'glTF',2,20+len(encoded)+len(tail)) + struct.pack('<II',len(encoded),kind) + encoded + tail
target.write_bytes(result)
(PACK/'expression_recipes.json').write_text(json.dumps({'recipes':recipes,'original_expressions':original},indent=2)+'\n')
# Verify only expressions changed, with byte-identical binary chunks.
check = copy.deepcopy(doc)
check['extensions']['VRMC_vrm']['expressions'] = original
assert check == json.loads(blob[20:20+length])
new_len = struct.unpack_from('<I',result,12)[0]
assert result[20+new_len:] == tail
for viseme in ['aa','ih','ou','ee','oh','blink','blinkLeft','blinkRight']:
    assert expressions['preset'][viseme] == original['preset'][viseme]
assert expressions['preset']['happy'] == original['preset']['happy']
report = {'status':'PASS','custom_expressions':list(custom),'only_expression_json_changed':True,
          'binary_chunks_unchanged':True,'visemes_and_blinks_unchanged':True,'original_happy_preserved':True,
          'original_sha256':hashlib.sha256(blob).hexdigest(),'revised_sha256':hashlib.sha256(result).hexdigest()}
(PACK/'expression_validation.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
