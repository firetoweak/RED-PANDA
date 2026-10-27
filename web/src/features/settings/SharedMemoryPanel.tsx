import { useGetSharedMemoryQuery, useSaveSharedMemoryMutation } from "../../api/kondenApi";
import { MemoryEditor } from "../memory/MemoryEditor";

export function SharedMemoryPanel({ active }: { active: boolean }) {
  const query = useGetSharedMemoryQuery(undefined, {
    skip: !active, pollingInterval: active ? 5000 : 0, refetchOnMountOrArgChange: true,
  });
  const [save, state] = useSaveSharedMemoryMutation();
  return <MemoryEditor kind="shared" query={query} save={(change) => save(change).unwrap()}
    saving={state.isLoading} saveError={state.error} reset={state.reset} />;
}
