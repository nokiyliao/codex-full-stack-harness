// Image models are hosted capabilities, not entries in the text-model picker.
// The operator selected Image 2.5 for the native tool's legacy image requests.
const IMAGE_MODELS = new Set([
  "gpt-image-2.5-flare", "gpt-image-2.5-flare-2026-09-08",
  "gpt-image-2.5-sunburst", "gpt-image-2.5-sunburst-2026-09-08",
]);
export function projectImageModel(body, endpoint) {
  if (endpoint !== "/images/generations" && endpoint !== "/images/edits") return null;
  if (!body || typeof body !== "object" || Array.isArray(body)) return null;
  const model = body.model === "gpt-image-2" ? "gpt-image-2.5-flare" : body.model;
  if (!IMAGE_MODELS.has(model)) return null;
  return { body: { ...body, model }, requestedModel: body.model };
}
