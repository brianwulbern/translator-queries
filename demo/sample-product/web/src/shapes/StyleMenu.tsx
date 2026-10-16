export function StyleMenu({overlay, onChange}) {
	return (
		<FormGroup>
			<label htmlFor="shape-style">{I18n.get('style')}</label>

			<Select
				id="shape-style"
				onChange={(event) =>
					onChange({sketchy: event.target.value === 'sketchy'})
				}
				options={[
					{
						label: I18n.get('clean'),
						value: 'clean',
					},
					{
						label: I18n.get('hand-drawn'),
						value: 'sketchy',
					},
				]}
				value={overlay.sketchy ? 'sketchy' : 'clean'}
			/>
		</FormGroup>
	);
}
