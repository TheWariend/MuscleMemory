import {useEffect, useState} from 'react';
import type {Client, ProcedureDetail, ProcedureSummary} from './api';

// Full skill records are small and change rarely; keep them per page load.
const cache = new Map<string, ProcedureDetail>();

/** Loads full details for up to `limit` skills, reusing anything already loaded. */
export function useSkillDetails(client: Client, procedures: ProcedureSummary[] | undefined, limit = 120) {
  const [details, setDetails] = useState<ProcedureDetail[]>();
  const [error, setError] = useState('');
  useEffect(() => {
    if (!procedures) return;
    const controller = new AbortController();
    const wanted = procedures.slice(0, limit);
    setError('');
    Promise.all(wanted.map(p => {
      const key = `${p.procedure_token}:${p.version}`;
      const hit = cache.get(key);
      return hit ? Promise.resolve(hit) : client.procedure(p.procedure_token, controller.signal).then(d => {cache.set(key, d); return d;});
    }))
      .then(setDetails)
      .catch(e => {if (!controller.signal.aborted) setError((e as Error).message);});
    return () => controller.abort();
  }, [client, procedures, limit]);
  return {details, error};
}

type Experience = {reads?: string[]; writes?: string[]; verification_commands?: string[]};

export function experienceOf(detail: ProcedureDetail): Required<Experience> {
  const e = ((detail.definition as unknown as {experience?: Experience}).experience) ?? {};
  return {reads: e.reads ?? [], writes: e.writes ?? [], verification_commands: e.verification_commands ?? []};
}
