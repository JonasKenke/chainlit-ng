import { type IVideoElement } from '@chainlit-ng/react-client';
import ReactPlayer from 'react-player';

const VideoElement = ({ element }: { element: IVideoElement }) => {
  if (!element.url) {
    return null;
  }

  return (
    <ReactPlayer
      className={`${element.display}-video`}
      width="100%"
      controls
      src={element.url}
      config={element.playerConfig || {}}
    />
  );
};

export { VideoElement };
