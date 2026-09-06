# Saved category art

The Saved screen uses a consistent Apple Music–inspired category-tile treatment.
Every category starts with a neutral image; the app, rather than the image,
applies the category colour.

## Creating art for a new category

Generate one static, people-free editorial image for the category's core
subject. It should be a simple, recognisable still life or scene, with no
words, logos, UI, borders, collage, or busy background. Compose it with the
subject comfortably inside a wide frame, since the app crops it to a 3:2 tile.

Use this prompt as a starting point:

> Minimal editorial still life representing **[CATEGORY]**: **[SUBJECT]**.
> No people, no text, no logo, no border, no collage. Clean simple background,
> strong recognisable silhouette, refined contemporary magazine photography.
> Wide horizontal composition with the subject safely away from every edge.
> Neutral lighting and muted colour; the app will add the final colour treatment.

Export a 1000 by 562 PNG. The existing tiles use that size.

## Adding the asset

1. Create `ios/PlaceLogger/Assets.xcassets/category-[slug].imageset/`.
2. Put the PNG at `art.png` inside it and use the same `Contents.json` shape as
   an existing `category-*.imageset`.
3. Add the category to `SavedCategory.known` in
   `ios/PlaceLogger/SavedCategory.swift`, pointing `artAssetName` to
   `category-[slug]`.
4. Add its type to `artTint` in that same file, reusing an existing colour when
   it belongs to the same visual family. Add a new tint only when it genuinely
   needs its own family.
5. Set `isPlaceBased` correctly. Place-based categories get the **View in
   Around Me** action; non-place categories do not.

## Do not bake the tint into the image

`SavedCategoryTile` renders the asset with `saturation(0)` and then
`colorMultiply(category.artTint)`, followed by a subtle bottom text gradient.
Keeping source art neutral preserves the coherent coloured, monochrome look
across every category and allows tint changes without regenerating imagery.
