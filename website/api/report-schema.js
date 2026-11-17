// Holds a report to report-schema.json, which tools/repo/report_schema.py
// writes from the tool's own constants. Only the JSON Schema words that file
// uses, as its validate() reads them: type, enum, pattern, maxLength,
// minimum, maximum, properties, required, additionalProperties,
// propertyNames, maxProperties. Both are held to test/fixtures/reports.json.

const TYPES = {
  string: (value) => typeof value === "string",
  integer: (value) => Number.isInteger(value),
  boolean: (value) => typeof value === "boolean",
  object: (value) => typeof value === "object" && value !== null && !Array.isArray(value),
  null: (value) => value === null,
};

// What is wrong with `value` by `rule`: [] when it fits.
export function validate(rule, value, where = "report") {
  if (rule.type !== undefined) {
    const allowed = Array.isArray(rule.type) ? rule.type : [rule.type];
    if (!allowed.some((name) => TYPES[name](value))) {
      return [`${where}: not ${allowed.join(" or ")}`];
    }
  }
  if (rule.enum !== undefined && !rule.enum.includes(value)) {
    return [`${where}: not one of the allowed words`];
  }
  const problems = [];
  if (typeof value === "string") {
    if (rule.maxLength !== undefined && value.length > rule.maxLength) {
      problems.push(`${where}: too long`);
    }
    if (rule.pattern !== undefined && !new RegExp(rule.pattern).test(value)) {
      problems.push(`${where}: not as it should be`);
    }
  }
  if (Number.isInteger(value)) {
    if ((rule.minimum !== undefined && value < rule.minimum) || (rule.maximum !== undefined && value > rule.maximum)) {
      problems.push(`${where}: out of range`);
    }
  }
  if (TYPES.object(value)) {
    const keys = Object.keys(value);
    if (rule.maxProperties !== undefined && keys.length > rule.maxProperties) {
      problems.push(`${where}: too many`);
    }
    const properties = rule.properties || {};
    for (const key of rule.required || []) {
      if (!Object.hasOwn(value, key)) {
        problems.push(`${where}.${key}: missing`);
      }
    }
    for (const key of keys) {
      if (rule.propertyNames !== undefined) {
        problems.push(...validate(rule.propertyNames, key, `${where} name ${key.slice(0, 16)}`));
      }
      if (Object.hasOwn(properties, key)) {
        problems.push(...validate(properties[key], value[key], `${where}.${key}`));
      } else if (rule.additionalProperties === false) {
        problems.push(`${where}.${key.slice(0, 16)}: not allowed`);
      } else if (TYPES.object(rule.additionalProperties)) {
        problems.push(...validate(rule.additionalProperties, value[key], `${where}.${key}`));
      }
    }
  }
  return problems;
}
