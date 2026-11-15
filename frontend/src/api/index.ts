import getRouterBasename from '@/lib/router';
import { ChainlitAPI, ClientError } from '@chainlit-ng/react-client';
import { toast } from 'sonner';

const devServer =
  (import.meta.env.VITE_API_URL || 'http://localhost:8000') +
  getRouterBasename();
const url = import.meta.env.DEV
  ? devServer
  : window.origin + getRouterBasename();
const serverUrl = new URL(url);

const httpEndpoint = serverUrl.toString();

const on401 = () => {
  if (window.location.pathname !== getRouterBasename() + '/login') {
    // The credentials aren't correct, remove the token and redirect to login
    window.location.href = getRouterBasename() + '/login';
  }
};

const onError = (error: ClientError) => {
  // Version panels surface these inline and recover by reloading the picker.
  if (error.status === 404 && error.path?.includes('/versions')) return;
  toast.error(error.toString());
};

class ExtendedChainlitAPI extends ChainlitAPI {
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
}

export const apiClient = new ExtendedChainlitAPI(
  httpEndpoint,
  'webapp',
  {}, // Optional - additionalQueryParams property.
  on401,
  onError
);
