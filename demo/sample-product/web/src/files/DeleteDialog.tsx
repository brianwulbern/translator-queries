import {announce} from '../a11y';

export async function deleteSelected(selection) {
	await selection.removeAll();

	announce(sub(I18n.get('x-files-deleted'), [selection.size]));
}
