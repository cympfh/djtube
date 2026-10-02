import { dispatch } from '../dispatch';
import { keyToAction, type KeyContext } from './keyboard';

export function bindKeyboard(getContext: () => KeyContext): () => void {
  const onKeyDown = (event: KeyboardEvent) => {
    if (event.isComposing || event.key === 'Process' || event.keyCode === 229) return;
    const action = keyToAction(event, getContext());
    if (!action) return;
    event.preventDefault();
    dispatch(action);
  };
  window.addEventListener('keydown', onKeyDown);
  return () => window.removeEventListener('keydown', onKeyDown);
}
