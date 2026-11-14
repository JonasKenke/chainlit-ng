import { ChainlitAPI, ClientError } from '@chainlit-ng/react-client';
import { toast } from 'sonner';

export function makeApiClient(
  chainlitServer: string,
  additionalQueryParams: Record<string, string>
) {
  const httpEndpoint = chainlitServer;

  const on401 = () => {
    toast.error('Unauthorized');
  };

  const onError = (error: ClientError) => {
    toast.error(error.toString());
  };

  return new ChainlitAPI(
    httpEndpoint,
    'copilot',
    additionalQueryParams,
    on401,
    onError
  );
}
