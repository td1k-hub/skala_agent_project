// Presentation cleanup also covers replies saved before metadata cleanup existed.
export function displayChatText(text = '') {
  return text.replace(/(?:\s*[（(]\s*)?source_ids?\s*:\s*\[[^\]]*\]\s*[)）]?/gi, '').trim()
}
