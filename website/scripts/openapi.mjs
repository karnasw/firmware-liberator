// The API's description (api/openapi.json), for the tests that hold the
// Worker to it (test/openapi.test.mjs): validate() checks a value against one
// of its schemas, and incompatible() lists what a new description would break
// for a client written against an older one. Only the JSON Schema words the
// description and report-schema.json use: type, enum, const, pattern,
// minLength, maxLength, minimum, maximum, minItems, maxItems, items,
// properties, required, additionalProperties, propertyNames, maxProperties,
// anyOf and $ref (within the file, or to a file beside it). report-schema.json's
// maxBytes is held as a bound too.
import { readFileSync } from "node:fs";

const API = new URL("../api/", import.meta.url);

export const loadSpec = (url = new URL("openapi.json", API)) => JSON.parse(readFileSync(url, "utf8"));

// What `ref` names, from `spec`: "#/a/b" within it, "file.json" or
// "file.json#/a/b" beside it.
export function resolve(spec, ref, base = API) {
  const [file, pointer = ""] = ref.split("#");
  const doc = file ? JSON.parse(readFileSync(new URL(file, base), "utf8")) : spec;
  return pointer.split("/").filter(Boolean).reduce((node, part) => node?.[part.replaceAll("~1", "/").replaceAll("~0", "~")], doc);
}

// `schema` with any $ref followed, and the document a further $ref is read in.
function deref(spec, schema) {
  let node = schema;
  let doc = spec;
  for (let hops = 0; node?.$ref !== undefined; hops += 1) {
    if (hops > 20) {
      throw new Error(`$ref loop at ${node.$ref}`);
    }
    const [file] = node.$ref.split("#");
    const next = resolve(doc, node.$ref);
    if (next === undefined) {
      throw new Error(`no ${node.$ref}`);
    }
    if (file) {
      doc = JSON.parse(readFileSync(new URL(file, API), "utf8"));
    }
    node = next;
  }
  return [node ?? {}, doc];
}

const TYPES = {
  string: (value) => typeof value === "string",
  integer: (value) => Number.isInteger(value),
  number: (value) => typeof value === "number",
  boolean: (value) => typeof value === "boolean",
  object: (value) => typeof value === "object" && value !== null && !Array.isArray(value),
  array: (value) => Array.isArray(value),
  null: (value) => value === null,
};
const typesOf = (schema) => (schema.type === undefined ? null : [schema.type].flat());

// What is wrong with `value` by `schema`: [] when it fits.
export function validate(spec, schema, value, where = "$") {
  const [rule, doc] = deref(spec, schema);
  const problems = [];
  const types = typesOf(rule);
  if (types && !types.some((name) => TYPES[name](value))) {
    return [`${where}: not ${types.join(" or ")}`];
  }
  if (rule.const !== undefined && value !== rule.const) {
    problems.push(`${where}: not ${JSON.stringify(rule.const)}`);
  }
  if (rule.enum !== undefined && !rule.enum.includes(value)) {
    problems.push(`${where}: not one of ${JSON.stringify(rule.enum)}`);
  }
  if (rule.anyOf !== undefined && !rule.anyOf.some((branch) => !validate(doc, branch, value, where).length)) {
    problems.push(`${where}: fits none of anyOf`);
  }
  if (typeof value === "string") {
    if (rule.maxLength !== undefined && value.length > rule.maxLength) problems.push(`${where}: too long`);
    if (rule.minLength !== undefined && value.length < rule.minLength) problems.push(`${where}: too short`);
    if (rule.pattern !== undefined && !new RegExp(rule.pattern, "u").test(value)) {
      problems.push(`${where}: does not match ${rule.pattern}`);
    }
  }
  if (typeof value === "number") {
    if (rule.maximum !== undefined && value > rule.maximum) problems.push(`${where}: over ${rule.maximum}`);
    if (rule.minimum !== undefined && value < rule.minimum) problems.push(`${where}: under ${rule.minimum}`);
  }
  if (Array.isArray(value)) {
    if (rule.maxItems !== undefined && value.length > rule.maxItems) problems.push(`${where}: too many`);
    if (rule.minItems !== undefined && value.length < rule.minItems) problems.push(`${where}: too few`);
    if (rule.items !== undefined) {
      value.forEach((item, index) => problems.push(...validate(doc, rule.items, item, `${where}[${index}]`)));
    }
  }
  if (TYPES.object(value)) {
    const keys = Object.keys(value);
    const properties = rule.properties ?? {};
    if (rule.maxProperties !== undefined && keys.length > rule.maxProperties) problems.push(`${where}: too many`);
    for (const key of rule.required ?? []) {
      if (!Object.hasOwn(value, key)) problems.push(`${where}.${key}: missing`);
    }
    for (const key of keys) {
      if (rule.propertyNames !== undefined) problems.push(...validate(doc, rule.propertyNames, key, `${where} name`));
      if (Object.hasOwn(properties, key)) {
        problems.push(...validate(doc, properties[key], value[key], `${where}.${key}`));
      } else if (rule.additionalProperties === false) {
        problems.push(`${where}.${key}: not allowed`);
      } else if (TYPES.object(rule.additionalProperties)) {
        problems.push(...validate(doc, rule.additionalProperties, value[key], `${where}.${key}`));
      }
    }
  }
  return problems;
}

// Bounds a narrower schema keeps within: an upper bound no higher, a lower no lower.
const UPPER = ["maxLength", "maximum", "maxItems", "maxProperties", "maxBytes"];
const LOWER = ["minLength", "minimum", "minItems"];
const isOpen = (rule) => Object.keys(rule).every((key) => ["description", "title", "example", "examples", "format"]
  .includes(key) || key.startsWith("x-"));

// Why not every value `narrow` takes is one `wide` takes too, as far as can be
// told from the words alone (two different patterns count as different):
// [] when every one is. With `extraTaken`, a property `wide` does not name is
// taken anyway, as a client reading an answer ignores one it does not know.
export function narrowerProblems(spec, narrow, wide, where = "$", specWide = spec, extraTaken = false) {
  const [a, docA] = deref(spec, narrow);
  const [b, docB] = deref(specWide, wide);
  if (isOpen(b)) {
    return [];
  }
  if (a.anyOf) {
    return a.anyOf.flatMap((branch, index) => narrowerProblems(docA, branch, b, `${where}|${index}`, docB, extraTaken));
  }
  if (b.anyOf) {
    const fits = b.anyOf.some((branch) => !narrowerProblems(docA, a, branch, where, docB, extraTaken).length);
    return fits ? [] : [`${where}: fits none of what it may be`];
  }
  // A few values alone: each checked as it is.
  const fixed = a.const !== undefined ? [a.const] : a.enum;
  if (fixed) {
    return fixed.filter((value) => validate(docB, b, value).length)
      .map((value) => `${where}: ${JSON.stringify(value)}, which it was not`);
  }
  const problems = [];
  const [typesA, typesB] = [typesOf(a), typesOf(b)];
  if (typesB && !(typesA && typesA.every((name) => typesB.includes(name)
      || (name === "integer" && typesB.includes("number"))))) {
    problems.push(`${where}: type ${JSON.stringify(typesA ?? "any")} where ${JSON.stringify(typesB)} was`);
  }
  const allowed = b.const !== undefined ? [b.const] : b.enum;
  if (allowed) {
    problems.push(`${where}: anything where only ${JSON.stringify(allowed)} was`);
  }
  if (b.pattern !== undefined && a.pattern !== b.pattern) {
    problems.push(`${where}: pattern ${a.pattern ?? "none"} where ${b.pattern} was`);
  }
  for (const bound of UPPER) {
    if (b[bound] !== undefined && !(a[bound] !== undefined && a[bound] <= b[bound])) {
      problems.push(`${where}: ${bound} ${a[bound] ?? "none"} where ${b[bound]} was`);
    }
  }
  for (const bound of LOWER) {
    if (b[bound] !== undefined && !(a[bound] !== undefined && a[bound] >= b[bound])) {
      problems.push(`${where}: ${bound} ${a[bound] ?? "none"} where ${b[bound]} was`);
    }
  }
  for (const key of b.required ?? []) {
    if (!(a.required ?? []).includes(key)) {
      problems.push(`${where}.${key}: may be missing where it was required`);
    }
  }
  const [propsA, propsB] = [a.properties ?? {}, b.properties ?? {}];
  for (const key of new Set([...Object.keys(propsA), ...Object.keys(propsB)])) {
    const inA = Object.hasOwn(propsA, key);
    const inB = Object.hasOwn(propsB, key);
    if (inA && inB) {
      problems.push(...narrowerProblems(docA, propsA[key], propsB[key], `${where}.${key}`, docB, extraTaken));
    } else if (inA && b.additionalProperties === false && !extraTaken) {
      problems.push(`${where}.${key}: may be given where it was not allowed`);
    } else if (inA && TYPES.object(b.additionalProperties)) {
      problems.push(...narrowerProblems(docA, propsA[key], b.additionalProperties, `${where}.${key}`, docB, extraTaken));
    } else if (inB && a.additionalProperties !== false && !isOpen(deref(docB, propsB[key])[0])) {
      problems.push(`${where}.${key}: may be anything where it was held to a schema`);
    }
  }
  if (b.additionalProperties === false && a.additionalProperties !== false && !extraTaken) {
    problems.push(`${where}: may have properties of any name where it had only those named`);
  }
  if (b.items !== undefined) {
    problems.push(...(a.items === undefined ? [`${where}[]: anything where it was held to a schema`]
      : narrowerProblems(docA, a.items, b.items, `${where}[]`, docB, extraTaken)));
  }
  return problems;
}

const METHODS = ["get", "put", "post", "delete", "patch", "head", "options"];
const contentOf = (spec, holder) => deref(spec, holder ?? {})[0].content ?? {};

// What `next` would break for a client written against `published`: every
// path and method kept; no request it could send turned away (a request's
// schema, parameters and size only widened); and no answer it could not read
// (a success status, content type or answer's schema only narrowed, never
// one added). [] when nothing.
export function incompatible(published, next) {
  const problems = [];
  for (const [path, item] of Object.entries(published.paths)) {
    for (const method of METHODS.filter((name) => item[name])) {
      const where = `${method.toUpperCase()} ${path}`;
      const [old, now] = [item[method], next.paths[path]?.[method]];
      if (!now) {
        problems.push(`${where}: gone`);
        continue;
      }
      const params = (op, spec) => new Map((op.parameters ?? []).map((param) => deref(spec, param)[0])
        .map((param) => [`${param.in} ${param.name}`, param]));
      const [oldParams, newParams] = [params(old, published), params(now, next)];
      for (const [name, param] of newParams) {
        const before = oldParams.get(name);
        if (param.required && !before?.required) {
          problems.push(`${where}: ${name} required where it was not`);
        }
        if (before && param.schema && before.schema) {
          problems.push(...narrowerProblems(published, before.schema, param.schema, `${where} ${name}`, next));
        }
        for (const [bound, higher] of [["x-proof-bits", false], ["x-proof-age", true]]) {
          if (before?.[bound] !== undefined && (higher ? param[bound] < before[bound] : param[bound] > before[bound])) {
            problems.push(`${where}: ${name} ${bound} ${param[bound]} where it was ${before[bound]}`);
          }
        }
      }
      const [oldBody, newBody] = [deref(published, old.requestBody ?? {})[0], deref(next, now.requestBody ?? {})[0]];
      if (newBody.required && !oldBody.required) {
        problems.push(`${where}: a body required where it was not`);
      }
      if (oldBody["x-max-bytes"] !== undefined && !(newBody["x-max-bytes"] >= oldBody["x-max-bytes"])) {
        problems.push(`${where}: a body of at most ${newBody["x-max-bytes"]} bytes where ${oldBody["x-max-bytes"]} was`);
      }
      if (oldBody["x-proof-bits"] !== undefined && !(newBody["x-proof-bits"] <= oldBody["x-proof-bits"])) {
        problems.push(`${where}: proof of ${newBody["x-proof-bits"]} bits where ${oldBody["x-proof-bits"]} was`);
      }
      if (oldBody["x-proof-age"] !== undefined && !(newBody["x-proof-age"] >= oldBody["x-proof-age"])) {
        problems.push(`${where}: proof good for ${newBody["x-proof-age"]} s where ${oldBody["x-proof-age"]} was`);
      }
      const [oldTypes, newTypes] = [contentOf(published, old.requestBody), contentOf(next, now.requestBody)];
      for (const [type, media] of Object.entries(oldTypes)) {
        if (!newTypes[type]) {
          problems.push(`${where}: no longer takes ${type}`);
        } else if (media.schema && newTypes[type].schema) {
          problems.push(...narrowerProblems(published, media.schema, newTypes[type].schema, `${where} body`, next));
        }
      }
      for (const [status, response] of Object.entries(now.responses)) {
        const before = old.responses[status];
        if (!before) {
          if (status.startsWith("2")) {
            problems.push(`${where}: answers ${status}, which it never did`);
          }
          continue;
        }
        const [oldMedia, newMedia] = [contentOf(published, before), contentOf(next, response)];
        for (const [type, media] of Object.entries(newMedia)) {
          if (!oldMedia[type]) {
            problems.push(`${where} ${status}: answers in ${type}, which it never did`);
          } else if (media.schema && oldMedia[type].schema) {
            problems.push(...narrowerProblems(next, media.schema, oldMedia[type].schema, `${where} ${status}`, published,
              true));
          }
        }
      }
      for (const status of Object.keys(old.responses).filter((code) => code.startsWith("2"))) {
        if (!now.responses[status]) {
          problems.push(`${where}: no longer answers ${status}`);
        }
      }
    }
  }
  return problems;
}
