import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { SchedulePayload, ScheduledTask } from '@/api/schedules'
import { schedulesApi } from '@/api/schedules'

export const useSchedulesStore = defineStore('schedules', () => {
  const tasks = ref<ScheduledTask[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)

  // Seq guard: rapid project A→B switching fires overlapping load()s — a
  // stale response for A must not overwrite the current project's list.
  let loadSeq = 0

  async function load(projectId: string) {
    const seq = ++loadSeq
    loading.value = true
    error.value = null
    try {
      const list = await schedulesApi.list(projectId)
      if (seq === loadSeq) tasks.value = list
    } catch (e: unknown) {
      if (seq === loadSeq) error.value = e instanceof Error ? e.message : 'Failed to load scheduled tasks'
    } finally {
      if (seq === loadSeq) loading.value = false
    }
  }

  /** Run a mutation, then reload unless the project switched mid-flight
   *  (reset() bumped loadSeq — a reload now would write the old project's
   *  list over the new project's store). */
  async function mutate(projectId: string, fn: () => Promise<unknown>): Promise<ScheduledTask | null> {
    const seq = loadSeq
    const result = await fn()
    if (seq === loadSeq) await load(projectId)
    return (result as ScheduledTask) ?? null
  }

  const create = (projectId: string, payload: SchedulePayload) =>
    mutate(projectId, () => schedulesApi.create(projectId, payload))

  const update = (projectId: string, scheduleId: string, patch: Partial<SchedulePayload>) =>
    mutate(projectId, () => schedulesApi.update(scheduleId, patch))

  const remove = (projectId: string, scheduleId: string) =>
    mutate(projectId, () => schedulesApi.remove(scheduleId))

  /** Enable/disable without touching the rest of the task. */
  const setEnabled = (projectId: string, scheduleId: string, enabled: boolean) =>
    mutate(projectId, () => schedulesApi.update(scheduleId, { enabled }))

  /** Fire once now; the schedule itself is unchanged (run history is reloaded
   *  by the caller after the run has had time to register). */
  const runNow = (scheduleId: string) => schedulesApi.runNow(scheduleId)

  function reset() {
    tasks.value = []
    loading.value = false
    error.value = null
    // Invalidate any in-flight load: without this, a load() started for the
    // previous project would still match its seq and write the old project's
    // task list into the freshly reset store.
    loadSeq++
  }

  return { tasks, loading, error, load, create, update, remove, setEnabled, runNow, reset }
})
