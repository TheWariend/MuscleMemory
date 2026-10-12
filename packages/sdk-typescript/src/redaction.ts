/** Copy JSON data while removing common secret fields and credential patterns. */
const sensitiveKey = /password|passwd|secret|authorization|cookie|api[_-]?key|access[_-]?token|refresh[_-]?token|private[_-]?key|credential|client[_-]?secret|^token$|^auth$/i;
const patterns = [
  /-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/g,
  /\bBearer\s+[^\s"'<>]+/gi,
  /\b(?:sk-[a-zA-Z0-9_-]{8,}|gh[pousr]_[a-zA-Z0-9]{16,}|AKIA[A-Z0-9]{16}|mm_[a-zA-Z0-9_-]{20,})\b/g,
  /\beyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\b/g,
  /\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|authorization)\s*[=:]\s*["']?[^\s,;"']+/gi,
  /(?<=:\/\/)[^/\s:@]+:[^/\s@]+@/gi,
];

export function redact(value: unknown, depth = 0): unknown {
  if (depth > 20) throw new TypeError('Capture payload nesting exceeds 20 levels');
  if (value === null || typeof value === 'boolean') return value;
  if (typeof value === 'string') {
    for (const pattern of patterns) value = (value as string).replace(pattern, '[REDACTED]');
    return value;
  }
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value !== 'object') throw new TypeError('Capture expects JSON data');
  const prototype = Object.getPrototypeOf(value);
  if (!Array.isArray(value) && prototype !== Object.prototype && prototype !== null) {
    throw new TypeError('Capture expects plain JSON objects');
  }
  const descriptors = Object.getOwnPropertyDescriptors(value);
  // Never invoke an accessor or toJSON method while collecting telemetry.
  if (Object.values(descriptors).some(item => item.get || item.set)) {
    throw new TypeError('Capture omits accessor properties');
  }
  if (Array.isArray(value)) return value.map(item => redact(item, depth + 1));
  const clean: Record<string, unknown> = Object.create(null);
  for (const [key, descriptor] of Object.entries(descriptors)) {
    if (!descriptor.enumerable) continue;
    if (descriptor.value === undefined) continue;
    clean[key] = sensitiveKey.test(key) ? '[REDACTED]' : redact(descriptor.value, depth + 1);
  }
  return clean;
}

export function safeCapture(value: unknown): unknown {
  try { return redact(value); }
  catch { return {capture_note: 'Non-JSON or overly nested data omitted', $mm_capture_omitted: true}; }
}

/** Conservative size estimate, including ASCII escapes and formatting overhead. */
export function captureSize(value: unknown): number {
  return JSON.stringify(value, null, 1).replace(/[\u007f-\uffff]/g,
    character => `\\u${character.charCodeAt(0).toString(16).padStart(4, '0')}`).length;
}
