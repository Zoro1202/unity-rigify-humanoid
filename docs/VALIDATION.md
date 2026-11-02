# Validation of 0.1.0

- Windows Unity 6000.3.9f1: **13/13 package EditMode tests passed**, no skips. Package-path resolution regression observed RED (12 pass / 1 fail) before implementing the resolver, then GREEN (13 pass).
- Installed Blender 5.2.3 LTS: **14/14 maintainer reference-profile regressions passed**. These need matching external reference fixtures, not distributed assets.
- **4/4 actual production package conversions passed**: a private reference character and three generated variants (split mesh/changed proportions, variable axial chain, unweighted optional DEF bones). Valid Human Avatars, preserved material references/source hashes, null controllers and temporary Humanoid retarget motion checked.
- Generated variants are not independently authored characters. No claim of universal Rigify compatibility, all animation poses/Foot IK/root motion, or native GUI visual QA.
- Public file audit excludes models, textures, animations, binaries, game data, private absolute paths and credentials. Independent publication review approved after clarifying fixture-specific tests and removing the private reference hash.
- Blender and Unity are external prerequisites and not redistributed. No additional characters or third-party game assets are covered by this repository's MIT license.

Raw local logs and private fixtures are deliberately **not published**. The package includes runnable asset-independent Editor tests and the explicitly profile-specific Blender maintainer suite.
