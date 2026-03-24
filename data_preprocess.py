from preprocess import preprocesses

input_datadir = './train_img'
output_datadir = './aligned_img'

obj=preprocesses(input_datadir,output_datadir)
stats = obj.collect_data()

print('Total number of images: %d' % stats['total_images'])
print('Number of newly aligned images: %d' % stats['newly_aligned'])
print('Number of skipped existing aligned images: %d' % stats['skipped_existing'])
print('Number of failed alignments: %d' % stats['failed'])


