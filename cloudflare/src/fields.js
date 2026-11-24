import definitions from "./fields.json" with { type: "json" };

export const fields = definitions.map(({ name, source, unit }) => ({ name, source, unit }));
export const defaultFields = fields.slice(0, 2).map(({ name }) => name);
export const fieldPositions = new Map(fields.map(({ name }, index) => [name, index + 1]));
