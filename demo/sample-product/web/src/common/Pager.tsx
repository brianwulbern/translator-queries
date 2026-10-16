import {Pager as SharedPager} from '@acme/ui-pager';

export function Pager({page, total, onChange}) {
	return (
		<SharedPager
			active={page}
			labels={{
				link: I18n.get('go-to-page-x'),
			}}
			onChange={onChange}
			total={total}
		/>
	);
}
