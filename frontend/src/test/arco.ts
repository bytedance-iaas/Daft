// Helpers for driving Arco components from Testing Library.
import { screen, waitFor, within } from '@testing-library/react';
import type { UserEvent } from '@testing-library/user-event';

/** Opens the Select labelled `label` and picks the option whose text is `option`. */
export async function pick(user: UserEvent, label: string | RegExp, option: string | RegExp, root: HTMLElement = document.body): Promise<void> {
  const combos = within(root).getAllByRole('combobox', { name: label });
  await user.click(combos[0]);
  const popup = await waitFor(() => {
    const opts = screen.getAllByRole('option').filter((o) => (typeof option === 'string' ? o.textContent?.trim() === option : option.test(o.textContent ?? '')));
    if (!opts.length) throw new Error(`no option ${String(option)}`);
    return opts[opts.length - 1];
  });
  // Multiple-mode selects wrap each option; the click handler sits on the inner element.
  await user.click(popup.querySelector<HTMLElement>('.arco-select-option') ?? popup);
}

/** The open Arco Drawer titled `title` (Arco drawers carry no dialog role). */
export async function findDrawer(title: string): Promise<HTMLElement> {
  return waitFor(() => {
    const t = [...document.querySelectorAll('.arco-drawer-header-title')].find((e) => e.textContent?.trim() === title);
    const d = t?.closest('.arco-drawer');
    if (!(d instanceof HTMLElement)) throw new Error(`no drawer ${title}`);
    return d;
  });
}

/** Types into the input labelled `label`, replacing what was there. */
export async function fill(user: UserEvent, label: string | RegExp, text: string, root: HTMLElement = document.body): Promise<void> {
  const el = within(root).getAllByLabelText(label).find((x) => x.tagName === 'INPUT' || x.tagName === 'TEXTAREA') as HTMLInputElement | undefined;
  if (!el) throw new Error(`no input ${String(label)}`);
  await user.clear(el);
  if (text) await user.type(el, text);
}
