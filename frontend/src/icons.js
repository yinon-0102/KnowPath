const paths = {
 search: '<circle cx="10.8" cy="10.8" r="6.8"/><path d="m16 16 4.2 4.2"/>',
 plus: '<path d="M12 5v14M5 12h14"/>',
 arrow: '<path d="M5 12h14m-5-5 5 5-5 5"/>',
 chevron: '<path d="m9 5 7 7-7 7"/>',
 down: '<path d="m6 9 6 6 6-6"/>',
 close: '<path d="m6 6 12 12M6 18 18 6"/>',
 check: '<path d="m5 12 4.5 4.5L19 7"/>',
 book: '<path d="M12 5v15M3 4c4-1 6 0 9 2 3-2 5-3 9-2v14c-4-1-6 0-9 2-3-2-5-3-9-2Z"/>',
 layers: '<path d="m12 3 10 5-10 5L2 8Zm-9 9 9 5 9-5M3 16l9 5 9-5"/>',
 graph: '<circle cx="12" cy="12" r="3"/><circle cx="4" cy="5" r="2"/><circle cx="20" cy="5" r="2"/><circle cx="5" cy="20" r="2"/><circle cx="20" cy="19" r="2"/><path d="m6 7 4 3m4 0 4-3m-8 7-4 4m8-4 4 3"/>',
 calendar: '<rect x="3" y="5" width="18" height="16" rx="3"/><path d="M7 3v4m10-4v4M3 10h18m-14 5h3m4 0h3"/>',
 clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
 upload: '<path d="M12 16V3m-5 5 5-5 5 5M4 15v5a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-5"/>',
 file: '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9Zm0 0v6h6M8 13h8m-8 4h6"/>',
 settings: '<path d="M4 7h16M4 17h16"/><circle cx="8" cy="7" r="3" fill="currentColor" stroke="none"/><circle cx="16" cy="17" r="3" fill="currentColor" stroke="none"/>',
 spark: '<path d="m12 3 2.4 6.6L21 12l-6.6 2.4L12 21l-2.4-6.6L3 12l6.6-2.4ZM20 2v4m-2-2h4"/>',
 play: '<path d="m8 4 12 8-12 8Z"/>',
 pause: '<path d="M8 5v14M16 5v14"/>',
 menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
 refresh: '<path d="M20 7v5h-5M4 17v-5h5M6 6a8 8 0 0 1 13 3M5 15a8 8 0 0 0 13 3"/>',
 mail: '<rect x="3" y="5" width="18" height="14" rx="3"/><path d="m3 7 9 6 9-6"/>',
 help: '<circle cx="12" cy="12" r="9"/><path d="M9 9a3 3 0 0 1 6 0c0 2-3 2-3 4m0 3h.01"/>',
 external: '<path d="M14 3h7v7m0-7L10 14m0-10H5a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h13a2 2 0 0 0 2-2v-5"/>',
 target: '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
 minus: '<path d="M5 12h14"/>',
 reset: '<path d="M8 3H3v5m13-5h5v5M3 16v5h5m8 0h5v-5"/>',
 connection: '<path d="m9 15 6-6m-7 3-2 2a4 4 0 0 0 6 6l2-2m-2-12 2-2a4 4 0 0 1 6 6l-2 2"/>',
 leaf: '<path d="M20 3C6 2 2 9 5 16c7 5 16-1 15-13ZM4 21l11-12"/>'
};
export function icon(name, extra = '') {
  return `<svg class="icon ${extra}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.book}</svg>`;
}
export const logo = '<svg viewBox="0 0 32 36" fill="none" aria-hidden="true"><path d="M7 7v22M25 7 13 18l12 11M7 18h6" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/><circle cx="25" cy="7" r="2.8" fill="currentColor"/></svg>';
