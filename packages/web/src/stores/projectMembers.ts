import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { ProjectMember } from '@/types'
import { listProjectMembers, addProjectMember, removeProjectMember } from '@/api/projectMembers'

export const useProjectMembersStore = defineStore('projectMembers', () => {
  const members = ref<ProjectMember[]>([])
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
      const list = await listProjectMembers(projectId)
      if (seq === loadSeq) members.value = list
    } catch (e: unknown) {
      if (seq === loadSeq) error.value = e instanceof Error ? e.message : 'Failed to load members'
    } finally {
      if (seq === loadSeq) loading.value = false
    }
  }

  async function add(projectId: string, userId: string) {
    const seq = loadSeq
    await addProjectMember(projectId, userId)
    // Project may have switched while the add was in flight: reset()
    // bumped loadSeq, so a fresh load(projectId) here would write the old
    // project's list over the new project's store.
    if (seq === loadSeq) await load(projectId)
  }

  async function remove(projectId: string, userId: string) {
    const seq = loadSeq
    await removeProjectMember(projectId, userId)
    // Same seq guard as add(): a project switch mid-remove must not filter
    // the same user_id out of the freshly loaded list of the new project.
    if (seq === loadSeq) {
      members.value = members.value.filter((m) => m.user_id !== userId)
    }
  }

  function reset() {
    members.value = []
    loading.value = false
    error.value = null
    // Invalidate any in-flight load: without this, a load() started for the
    // previous project would still match its seq and write the old project's
    // member list into the freshly reset store.
    loadSeq++
  }

  return { members, loading, error, load, add, remove, reset }
})
