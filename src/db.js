export function parseCard(row) {
  if (!row) return null;
  return {
    ...row,
    parallel: Boolean(row.parallel),
    colors: row.colors ? JSON.parse(row.colors) : null,
    attributes: row.attributes ? JSON.parse(row.attributes) : null,
    types: row.types ? JSON.parse(row.types) : null,
    tcg_ids: row.tcg_ids ? JSON.parse(row.tcg_ids) : null,
    trigger: row.trigger_text,
    trigger_text: undefined,
  };
}

export function parseCards(rows) {
  return rows.map(parseCard);
}
