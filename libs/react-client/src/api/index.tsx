import {
  IElement,
  IMcpConnectResponse,
  IMcpPromptResponse,
  IMcpResourceResponse,
  IThread,
  IUser
} from 'src/types';

import { IAction } from 'src/types/action';
import {
  IApprovalDecisionPayload,
  IApprovalDecisionResponse
} from 'src/types/approval';
import { IFeedback } from 'src/types/feedback';
import { IPrompt } from 'src/types/prompt';
import {
  IScheduledTask,
  IScheduledTaskPayload,
  IScheduledTaskUpdate
} from 'src/types/scheduledTask';
import {
  ISkill,
  ISkillExport,
  ISkillPayload,
  ISkillUpdate
} from 'src/types/skill';

export * from './hooks/auth';
export * from './hooks/api';

export interface IThreadFilters {
  search?: string;
  feedback?: number;
}

export interface IPageInfo {
  hasNextPage: boolean;
  endCursor?: string;
}

export interface IPagination {
  first: number;
  cursor?: string | number;
}

export class ClientError extends Error {
  status: number;
  detail?: string;
  payload?: any;

  constructor(message: string, status: number, detail?: string, payload?: any) {
    super(message);
    this.status = status;
    this.detail = detail;
    this.payload = payload;
  }

  toString() {
    const detail: unknown = this.detail;
    if (!detail) return this.message;
    if (typeof detail === 'string') {
      return `${this.message}: ${detail}`;
    }
    // A structured 422 (e.g. approvals) carries {error, field, type}; render
    // the path and type instead of "[object Object]".
    const structured = detail as { field?: unknown; type?: unknown };
    if (typeof structured.field === 'string') {
      const type =
        typeof structured.type === 'string' ? ` (${structured.type})` : '';
      return `${this.message}: ${structured.field}${type}`;
    }
    return `${this.message}: ${JSON.stringify(detail)}`;
  }
}

type Payload = FormData | any;

export class APIBase {
  constructor(
    public httpEndpoint: string,
    public type: 'webapp' | 'copilot' | 'teams' | 'slack' | 'discord',
    public additionalQueryParams?: Record<string, string>,
    public on401?: () => void,
    public onError?: (error: ClientError) => void
  ) {}

  buildEndpoint(path: string) {
    let fullUrl = `${this.httpEndpoint}${path}`;
    if (this.httpEndpoint.endsWith('/')) {
      // remove trailing slash on httpEndpoint
      fullUrl = `${this.httpEndpoint.slice(0, -1)}${path}`;
    }

    const url = new URL(fullUrl);

    // Add additionalQueryParams for all API calls
    if (this.additionalQueryParams) {
      const params = new URLSearchParams(this.additionalQueryParams);
      const separator = url.search ? '&' : '?';
      url.search = url.search + `${separator}${params.toString()}`;
    }

    return url.toString();
  }

  private async getErrorResponse(res: Response): Promise<any> {
    try {
      return await res.json();
    } catch (error: any) {
      console.error('Unable to parse error response', error);
    }
    return undefined;
  }

  private handleRequestError(error: any) {
    if (error instanceof ClientError) {
      if (error.status === 401 && this.on401) {
        this.on401();
      }
      if (this.onError) {
        this.onError(error);
      }
    }
    console.error(error);
  }

  /**
   * Low-level HTTP request handler for direct API interactions.
   * Provides full control over HTTP methods, request configuration, and error handling.
   *
   * Key features:
   * - Supports all HTTP methods (GET, POST, PUT, PATCH, DELETE)
   * - Handles both FormData and JSON payloads
   * - Manages authentication headers
   * - Custom error handling with ClientError class
   * - Support for request cancellation via AbortSignal
   *
   * @param method - HTTP method to use (GET, POST, etc.)
   * @param path - API endpoint path
   * @param data - Optional request payload (FormData or JSON-serializable data)
   * @param signal - Optional AbortSignal for request cancellation
   * @returns Promise<Response>
   * @throws ClientError for HTTP errors, including 401 unauthorized
   */
  async fetch(
    method: string,
    path: string,
    data?: Payload,
    signal?: AbortSignal,
    headers: { Authorization?: string; 'Content-Type'?: string } = {}
  ): Promise<Response> {
    try {
      let body;

      if (data instanceof FormData) {
        body = data;
      } else {
        headers['Content-Type'] = 'application/json';
        body = data ? JSON.stringify(data) : null;
      }

      const res = await fetch(this.buildEndpoint(path), {
        method,
        credentials: 'include',
        headers,
        signal,
        body
      });

      if (!res.ok) {
        const payload = await this.getErrorResponse(res);

        throw new ClientError(
          res.statusText,
          res.status,
          payload?.detail,
          payload
        );
      }

      return res;
    } catch (error: any) {
      this.handleRequestError(error);
      throw error;
    }
  }

  async get(endpoint: string) {
    return await this.fetch('GET', endpoint);
  }

  async post(endpoint: string, data: Payload, signal?: AbortSignal) {
    return await this.fetch('POST', endpoint, data, signal);
  }

  async put(endpoint: string, data: Payload) {
    return await this.fetch('PUT', endpoint, data);
  }

  async patch(endpoint: string, data: Payload) {
    return await this.fetch('PATCH', endpoint, data);
  }

  async delete(endpoint: string, data: Payload) {
    return await this.fetch('DELETE', endpoint, data);
  }
}

export class ChainlitAPI extends APIBase {
  async headerAuth() {
    const res = await this.post(`/auth/header`, {});
    return res.json();
  }

  async jwtAuth(token: string) {
    const res = await this.fetch('POST', '/auth/jwt', undefined, undefined, {
      Authorization: `Bearer ${token}`
    });
    return res.json();
  }

  async stickyCookie(sessionId: string) {
    const res = await this.fetch('POST', '/set-session-cookie', {
      session_id: sessionId
    });
    return res.json();
  }

  async passwordAuth(data: FormData) {
    const res = await this.post(`/login`, data);
    return res.json();
  }

  async getUser(): Promise<IUser> {
    const res = await this.get(`/user`);
    return res.json();
  }

  async logout() {
    const res = await this.post(`/logout`, {});
    return res.json();
  }

  async setFeedback(
    feedback: IFeedback,
    sessionId: string
  ): Promise<{ success: boolean; feedbackId: string }> {
    const res = await this.put(`/feedback`, { feedback, sessionId });
    return res.json();
  }

  async deleteFeedback(feedbackId: string): Promise<{ success: boolean }> {
    const res = await this.delete(`/feedback`, { feedbackId });
    return res.json();
  }

  async listThreads(
    pagination: IPagination,
    filter: IThreadFilters
  ): Promise<{
    pageInfo: IPageInfo;
    data: IThread[];
  }> {
    const res = await this.post(`/project/threads`, { pagination, filter });

    return res.json();
  }

  async renameThread(threadId: string, name: string) {
    const res = await this.put(`/project/thread`, { threadId, name });

    return res.json();
  }

  async deleteThread(threadId: string) {
    const res = await this.delete(`/project/thread`, { threadId });

    return res.json();
  }

  async deleteAllThreads(): Promise<{ success: boolean; deleted: number }> {
    const res = await this.delete(`/project/threads`, {});

    return res.json();
  }

  uploadFile(
    file: File,
    onProgress: (progress: number) => void,
    sessionId: string,
    parentId?: string
  ) {
    const xhr = new XMLHttpRequest();
    xhr.withCredentials = true;

    const promise = new Promise<{ id: string }>((resolve, reject) => {
      const formData = new FormData();
      formData.append('file', file);

      const ask_parent_id = parentId ? `&ask_parent_id=${parentId}` : '';
      xhr.open(
        'POST',
        this.buildEndpoint(
          `/project/file?session_id=${sessionId}${ask_parent_id}`
        ),
        true
      );

      // Track the progress of the upload
      xhr.upload.onprogress = function (event) {
        if (event.lengthComputable) {
          const percentage = (event.loaded / event.total) * 100;
          onProgress(percentage);
        }
      };

      xhr.onload = function () {
        if (xhr.status === 200) {
          const response = JSON.parse(xhr.responseText);
          resolve(response);
          return;
        }
        const contentType = xhr.getResponseHeader('Content-Type');
        if (contentType && contentType.includes('application/json')) {
          const response = JSON.parse(xhr.responseText);
          reject(response.detail);
        } else {
          reject('Upload failed');
        }
      };

      xhr.onerror = function () {
        reject('Upload error');
      };

      xhr.send(formData);
    });

    return { xhr, promise };
  }

  async callAction(action: IAction, sessionId: string) {
    const res = await this.post(`/project/action`, { sessionId, action });

    return res.json();
  }

  async respondToApproval(
    payload: IApprovalDecisionPayload
  ): Promise<IApprovalDecisionResponse> {
    try {
      const res = await this.post(`/project/approval`, payload);
      return res.json();
    } catch (error) {
      // A 409 carries the terminal record so a stale card can reconcile
      // without waiting for another event.
      if (
        error instanceof ClientError &&
        error.status === 409 &&
        error.payload?.approvalId
      ) {
        return error.payload as IApprovalDecisionResponse;
      }
      throw error;
    }
  }

  async updateElement(element: IElement, sessionId: string) {
    const res = await this.put(`/project/element`, { sessionId, element });

    return res.json();
  }

  async deleteElement(element: IElement, sessionId: string) {
    const res = await this.delete(`/project/element`, { sessionId, element });

    return res.json();
  }

  async connectMcp(
    sessionId: string,
    name: string
  ): Promise<IMcpConnectResponse> {
    const res = await this.post(`/mcp`, { sessionId, name });
    return res.json();
  }

  async connectUserMcp(
    sessionId: string,
    name: string,
    type: 'sse' | 'streamable-http',
    url: string,
    headers?: Record<string, string>,
    useOAuth?: boolean
  ): Promise<IMcpConnectResponse> {
    const res = await this.post(`/mcp`, {
      sessionId,
      name,
      clientType: type,
      url,
      ...(headers ? { headers } : {}),
      ...(useOAuth ? { useOAuth: true } : {})
    });
    return res.json();
  }

  async disconnectMcp(sessionId: string, name: string) {
    const res = await this.delete(`/mcp`, { sessionId, name });
    return res.json();
  }

  /** Render one prompt published by a connected MCP server. */
  async getMcpPrompt(
    sessionId: string,
    name: string,
    prompt: string,
    args?: Record<string, string>
  ): Promise<IMcpPromptResponse> {
    const res = await this.post(`/mcp/prompt`, {
      sessionId,
      name,
      prompt,
      ...(args && Object.keys(args).length ? { arguments: args } : {})
    });
    return res.json();
  }

  /** Read one resource published by a connected MCP server. */
  async readMcpResource(
    sessionId: string,
    name: string,
    uri: string,
    resourceName?: string
  ): Promise<IMcpResourceResponse> {
    const res = await this.post(`/mcp/resource`, {
      sessionId,
      name,
      uri,
      ...(resourceName ? { resourceName } : {})
    });
    return res.json();
  }

  getElementUrl(id: string, sessionId: string) {
    const queryParams = `?session_id=${sessionId}`;
    return this.buildEndpoint(`/project/file/${id}${queryParams}`);
  }

  getLogoEndpoint(theme: string, configuredLogoUrl?: string) {
    if (configuredLogoUrl) {
      return configuredLogoUrl.startsWith('/public')
        ? this.buildEndpoint(configuredLogoUrl)
        : configuredLogoUrl;
    }
    return this.buildEndpoint(`/logo?theme=${theme}`);
  }

  getOAuthEndpoint(provider: string) {
    return this.buildEndpoint(`/auth/oauth/${provider}`);
  }
  async shareThread(
    threadId: string,
    isShared: boolean
  ): Promise<{ success: boolean }> {
    const res = await this.put(`/project/thread/share`, {
      threadId,
      isShared
    });
    return res.json();
  }

  // ---- Scheduled tasks ----

  async listScheduledTasks(): Promise<IScheduledTask[]> {
    const res = await this.get(`/project/scheduled-tasks`);
    return res.json();
  }

  async createScheduledTask(
    payload: IScheduledTaskPayload
  ): Promise<IScheduledTask> {
    const res = await this.post(`/project/scheduled-tasks`, payload);
    return res.json();
  }

  async updateScheduledTask(
    id: string,
    payload: IScheduledTaskUpdate
  ): Promise<IScheduledTask> {
    const res = await this.put(`/project/scheduled-tasks/${id}`, payload);
    return res.json();
  }

  async deleteScheduledTask(id: string): Promise<{ success: boolean }> {
    const res = await this.delete(`/project/scheduled-tasks/${id}`, {});
    return res.json();
  }

  async runScheduledTask(id: string): Promise<IScheduledTask> {
    const res = await this.post(`/project/scheduled-tasks/${id}/run`, {});
    return res.json();
  }

  // ---- Prompt Gallery ----

  async listPrompts(): Promise<IPrompt[]> {
    const res = await this.get(`/project/prompts`);
    return res.json();
  }

  async createPrompt(title: string, content: string): Promise<IPrompt> {
    const res = await this.post(`/project/prompts`, { title, content });
    return res.json();
  }

  async updatePrompt(
    id: string,
    fields: { title?: string; content?: string }
  ): Promise<IPrompt> {
    const res = await this.put(`/project/prompts/${id}`, fields);
    return res.json();
  }

  async deletePrompt(id: string): Promise<{ success: boolean }> {
    const res = await this.delete(`/project/prompts/${id}`, {});
    return res.json();
  }

  async sharePrompt(
    id: string,
    isShared: boolean
  ): Promise<{ prompt: IPrompt; shareUrl: string }> {
    const res = await this.post(`/project/prompts/${id}/share`, { isShared });
    return res.json();
  }

  async getSharedPrompt(id: string): Promise<IPrompt> {
    const res = await this.get(`/project/prompt/share/${id}`);
    return res.json();
  }

  async addSharedPrompt(id: string): Promise<IPrompt> {
    const res = await this.post(`/project/prompt/share/${id}/add`, {});
    return res.json();
  }

  // ---- Skills ----

  async listSkills(): Promise<ISkill[]> {
    const res = await this.get(`/project/skills`);
    return res.json();
  }

  async createSkill(payload: ISkillPayload): Promise<ISkill> {
    const res = await this.post(`/project/skills`, payload);
    return res.json();
  }

  async updateSkill(id: string, fields: ISkillUpdate): Promise<ISkill> {
    const res = await this.put(`/project/skills/${id}`, fields);
    return res.json();
  }

  async deleteSkill(id: string): Promise<{ success: boolean }> {
    const res = await this.delete(`/project/skills/${id}`, {});
    return res.json();
  }

  async importSkill(markdown: string, importedFrom: string): Promise<ISkill> {
    const res = await this.post(`/project/skills/import`, {
      markdown,
      imported_from: importedFrom
    });
    return res.json();
  }

  async exportSkill(id: string): Promise<ISkillExport> {
    const res = await this.get(`/project/skills/${id}/export`);
    const markdown = await res.text();
    const disposition = res.headers.get('Content-Disposition') || '';
    const match = /filename="([^"]+)"/.exec(disposition);
    return {
      markdown,
      filename: match?.[1] || `${id}.md`,
      contentHash: res.headers.get('X-Skill-Content-Hash') || undefined
    };
  }
}
