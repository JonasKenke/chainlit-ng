export interface ISkill {
  id: string;
  title: string;
  content: string;
  userId: string;
  isShared: boolean;
  createdAt: string;
  updatedAt: string;
  kind?: 'skill';
  description?: string;
  enabled?: boolean;
  importedFrom?: string | null;
  importedAt?: string | null;
  contentHash?: string | null;
}

export interface ISkillPayload {
  name: string;
  description: string;
  content: string;
  enabled?: boolean;
}

export interface ISkillUpdate {
  name?: string;
  description?: string;
  content?: string;
  enabled?: boolean;
}

export interface ISkillExport {
  markdown: string;
  filename: string;
  contentHash?: string;
}
