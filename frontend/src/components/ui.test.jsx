import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';

import { HistoryTypeBadge } from './ui';

describe('HistoryTypeBadge', () => {
  it.each([
    ['qbittorrent', 'qBIT', 'Downloaded via qBittorrent'],
    ['sabnzbd', 'SAB', 'Downloaded via SABnzbd'],
  ])('labels download client %s without a separate column', (clientType, label, title) => {
    const html = renderToStaticMarkup(
      <HistoryTypeBadge type="download" clientType={clientType} />,
    );

    expect(html).toContain('Download');
    expect(html).toContain(label);
    expect(html).toContain(title);
  });

  it.each([
    ['hevc_vaapi', true, 'VAAPI'],
    ['av1_qsv', true, 'QSV'],
    ['h264_nvenc', true, 'NVENC'],
    ['libx265', false, 'CPU'],
  ])('labels encoder %s with engine %s', (encoderUsed, hwaccelUsed, label) => {
    const html = renderToStaticMarkup(
      <HistoryTypeBadge
        type="encode"
        encoderUsed={encoderUsed}
        hwaccelUsed={hwaccelUsed}
      />,
    );

    expect(html).toContain('Encode');
    expect(html).toContain(label);
    expect(html).toContain(encoderUsed);
  });

  it('keeps legacy records compact when engine metadata is unavailable', () => {
    const html = renderToStaticMarkup(<HistoryTypeBadge type="encode" />);

    expect(html).toContain('Encode');
    expect(html).not.toContain('CPU');
    expect(html).not.toContain('HW');
  });
});
