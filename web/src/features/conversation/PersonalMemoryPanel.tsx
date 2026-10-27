import { useGetPersonalMemoryQuery, useSavePersonalMemoryMutation } from "../../api/kondenApi";
import { MemoryEditor } from "../memory/MemoryEditor";

export function PersonalMemoryPanel({ sessionId, active }: { sessionId: string; active: boolean }) {
  const query = useGetPersonalMemoryQuery(sessionId, {
    skip: !active, pollingInterval: active ? 5000 : 0, refetchOnMountOrArgChange: true,
  });
  const [save, state] = useSavePersonalMemoryMutation();
  return <MemoryEditor kind="personal" query={{ data: query.currentData, error: query.error, refetch: query.refetch }}
    save={(change) => save({ sessionId, ...change }).unwrap()} saving={state.isLoading} saveError={state.error} reset={state.reset} />;
}
