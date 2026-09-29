# PAW planet avatars

`planet-avatars-v1.png` is an RGBA atlas generated for PAW with OpenAI ImageGen
on 2026-09-26 from the user-approved planet character concept. It has no labels
or background. The stable identities are Earth, Mars, Venus, Jupiter, Saturn,
Mercury, Neptune and Uranus in the existing participant ordinal order.

`RoomPlanetAvatar.tsx` owns the individual viewports. Preserve the original
alpha, each identity and Saturn's full ring when changing the atlas. Do not use
these decorative avatars to imply that a participant is executing.

`planet-bodies-v1.png` is the matching ImageGen edit with only eyes, brows and
mouths removed (2026-09-26). The product renders it with separate SVG facial
features: blinking, gaze, mouth opening and state expressions. Face animation
uses the actual activity prop and respects reduced motion; historical avatars
remain static. `planet-avatars-v1.png` preserves the original visual reference.
