// Helpers for the 「所有表单必填项红 * + 校验」 checks (07 §3, §9).

/** Labels (text) that carry Arco's required mark (the red asterisk). */
export function requiredFieldLabels(root: ParentNode = document): string[] {
  return [...root.querySelectorAll('label')]
    .filter((l) => l.querySelector('.arco-form-item-symbol'))
    .map((l) => (l.textContent ?? '').replace(/\s+/g, ' ').trim());
}

/** Field error messages currently shown under form items. */
export function fieldErrors(root: ParentNode = document): string[] {
  return [...root.querySelectorAll('.arco-form-message')].map((e) => (e.textContent ?? '').trim()).filter(Boolean);
}
