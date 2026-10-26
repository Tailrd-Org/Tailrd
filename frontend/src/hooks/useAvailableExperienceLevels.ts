import { useEffect, useState } from "react";
import api from "../auth/api";
import { parseAvailableLevels } from "../lib/jobFilterOptions";

/**
 * The Experience Level options that have live jobs behind them (GET /jobs/levels), or
 * null while that is not known. Null also covers a request that failed: the Jobs page
 * and onboarding then offer all six options (availableExperienceOptions) instead of
 * losing the filter. The endpoint is cached for minutes on the server, so each page
 * simply asks once when it mounts.
 */
export function useAvailableExperienceLevels(): string[] | null {
  const [levels, setLevels] = useState<string[] | null>(null);

  useEffect(() => {
    let mounted = true;
    (async () => {
      try {
        const res = await api.get("/jobs/levels");
        if (mounted) setLevels(parseAvailableLevels(res?.data));
      } catch {
        // Not known: every option stays on offer.
      }
    })();
    return () => {
      mounted = false;
    };
  }, []);

  return levels;
}
