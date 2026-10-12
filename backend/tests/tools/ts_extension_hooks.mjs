// Node module-resolution hook for the profile-facts parity runner.
//
// The extension's TypeScript imports its siblings without a file extension ("./geo"), which Node's
// type stripping does not resolve on its own. For a relative import with no extension, try the
// ".ts" file first; anything else resolves as usual. Registered by ts_profile_facts_reference.mjs.

export async function resolve(specifier, context, nextResolve) {
  if (/^[.]{1,2}[/]/.test(specifier) && !/[.][A-Za-z0-9]+$/.test(specifier)) {
    try {
      return await nextResolve(specifier + ".ts", context);
    } catch {
      // not a .ts sibling: fall through to the default resolution (and its error)
    }
  }
  return nextResolve(specifier, context);
}
